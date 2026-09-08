"""Claude Limits - a Windows system-tray gauge for your Claude usage limits.

  .oooooo.   ooooo             .o.       ooooo     ooo oooooooooo.  oooooooooooo
 d8P'  `Y8b  `888'            .888.      `888'     `8' `888'   `Y8b `888'     `8
888           888            .8"888.      888       8   888     888  888
888           888           .8' `888.     888       8   888     888  888oooo8
888           888          .88ooo8888.    888       8   888     888  888    "
`88b    ooo   888       o .8'     `888.   `88.    .8'   888    d88'  888       o
 `Y8bood8P'  o888ooooood8 o88o     o8888o   `YbodP'    o888bood8P'  o888ooooood8

Shows the session percentage and the countdown to its reset, drawn inside the
tray icon itself. A two-bar style (session and weekly, no text) is available
from the menu. The numbers are account-wide, they cover claude.ai, the desktop
and mobile apps, and Claude Code, because those share one pool on Pro/Max.

Auth: none of its own. It re-reads the access token that Claude Code already
maintains at ~/.claude/.credentials.json on every poll, so it never stores a
secret and never has to run a login flow. The token is never logged.

Data source: GET https://api.anthropic.com/api/oauth/usage - an internal,
undocumented endpoint. Every field is read defensively; an unexpected shape
degrades to "unknown" rather than crashing the tray.
"""

from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pystray
from PIL import Image, ImageDraw

# --- (1) Constants -----------------------------------------------------------

CREDENTIALS_PATH = os.path.expanduser(r"~\.claude\.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-limits/1.0"

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ClaudeLimits")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

# Last good reading, shared across processes so a second instance started before
# the first exits reads it instead of polling again.
SNAPSHOT_PATH = os.path.join(SETTINGS_DIR, "snapshot.json")

# Rotating log. Under pythonw.exe there is no console and stderr is discarded, so
# without a file a failure leaves no trace - on 2026-08-25 the process was alive
# with an empty tray and nothing recorded why.
LOG_PATH = os.path.join(SETTINGS_DIR, "claude-limits.log")
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUPS = 2

log = logging.getLogger("claude_limits")


def setup_logging() -> None:
    """Attach the rotating file handler.

    Never raises: having no log is bad, but having no tray icon is worse. The
    access token is never handed to the logger - only outcomes and error text.
    """
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        return
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())

# A snapshot younger than this is reused rather than re-fetched, so a restart
# inside the window costs no request - the common case, since a manual launch on
# top of the login-started copy is routine.
CACHE_FRESH_SECONDS = 110

# How long to wait after a 429 when the server declines to say.
RATE_LIMIT_BACKOFF = 900

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "ClaudeLimits"

# Named mutex claimed at startup so a second copy exits instead of adding a second
# tray icon and a second poller. "Local\" scopes it to the logon session, which is
# the right scope for a tray icon: two users switched between keep one icon each.
MUTEX_NAME = r"Local\ClaudeLimits"

# Poll cadence, and the backoff ladder after consecutive failures: don't hammer
# the endpoint when it's unhappy.
BASE_INTERVAL = 180
BACKOFF_LADDER = ((3, 300), (6, 600))
MAX_INTERVAL = 1800

# Colors by how much of the window is USED (high = close to the limit). Two
# palettes: the taskbar can be light or dark, and a single palette that reads on
# one is muddy on the other. Which one applies is read from the registry.
PALETTE = {
    "dark": {
        "ok": (86, 205, 130),
        "warn": (236, 180, 70),
        "crit": (238, 104, 98),
        "track": (150, 150, 150, 70),
        "edge": (170, 170, 170, 190),
        "unknown": (165, 165, 165, 190),
        # Only slightly dimmer than the percentage: at 12px, contrast is legibility.
        "muted": (202, 202, 202),
    },
    "light": {
        "ok": (24, 132, 76),
        "warn": (163, 106, 8),
        "crit": (188, 44, 40),
        "track": (0, 0, 0, 48),
        "edge": (0, 0, 0, 90),
        "unknown": (0, 0, 0, 110),
        "muted": (48, 48, 48),
    },
}

WARN_AT = 60.0
CRIT_AT = 85.0

THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

# Candidate faces, best first. Bold keeps two digits legible at tray size.
FONT_CANDIDATES = ("segoeuib.ttf", "tahomabd.ttf", "arialbd.ttf", "seguisb.ttf")

# Condensed faces for the two-row layout, as (file, variable-font instance or None).
# "41" is 19px wide condensed against 24px in Segoe UI Bold - that is what makes it fit.
CONDENSED_CANDIDATES = (("bahnschrift.ttf", "Bold Condensed"), ("arialnb.ttf", None))

# Repaint cadence, independent of the poll cadence: the countdown has to tick down
# on its own, or the minutes sit visibly stale between polls.
REDRAW_INTERVAL = 20


# --- (2) Settings ------------------------------------------------------------


STYLE_NUMBER = "number"
STYLE_BARS = "bars"


@dataclass
class Settings:
    """User preferences, persisted as JSON next to nothing sensitive."""

    notify_enabled: bool = False
    # Notify when weekly REMAINING drops below this percentage.
    notify_threshold: int = 20
    icon_style: str = STYLE_NUMBER
    number_metric: str = "session"
    show_time: bool = True

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            style = str(data.get("icon_style", STYLE_NUMBER))
            metric = str(data.get("number_metric", "session"))
            return cls(
                notify_enabled=bool(data.get("notify_enabled", False)),
                notify_threshold=int(data.get("notify_threshold", 20)),
                icon_style=style if style in (STYLE_NUMBER, STYLE_BARS) else STYLE_NUMBER,
                number_metric=metric if metric in ("session", "weekly") else "session",
                show_time=bool(data.get("show_time", True)),
            )
        except (OSError, ValueError, TypeError):
            # Missing or corrupt settings must never stop the app starting.
            return cls()

    def save(self) -> None:
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump({
                    "notify_enabled": self.notify_enabled,
                    "notify_threshold": self.notify_threshold,
                    "icon_style": self.icon_style,
                    "number_metric": self.number_metric,
                    "show_time": self.show_time,
                }, fh, indent=2)
        except OSError:
            log.warning("Could not save settings to %s", SETTINGS_PATH,
                        exc_info=True)


# --- (3) Credentials ---------------------------------------------------------


class AuthError(Exception):
    """The token is missing, or the endpoint rejected it."""


def read_access_token() -> str:
    """Pull the access token out of Claude Code's credentials file.

    Walks the structure rather than hard-coding claudeAiOauth.accessToken, so a
    future reshuffle of that file doesn't break us outright.
    """
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as fh:
            creds = json.load(fh)
    except FileNotFoundError:
        raise AuthError("Claude Code is not signed in on this machine.") from None
    except (OSError, ValueError) as exc:
        raise AuthError(f"Could not read credentials: {type(exc).__name__}") from None

    found: list[str] = []

    def walk(node) -> None:
        if found:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and "accesstoken" in key.lower().replace("_", ""):
                    found.append(value)
                    return
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(creds)
    if not found:
        raise AuthError("No access token found in the credentials file.")
    return found[0]


# --- (4) Usage model ---------------------------------------------------------


@dataclass
class Gauge:
    """One limit window."""

    label: str
    used_percent: float | None = None
    resets_at: datetime | None = None

    @property
    def known(self) -> bool:
        return self.used_percent is not None

    def reset_text(self) -> str:
        if self.resets_at is None:
            return ""
        delta = self.resets_at - datetime.now(timezone.utc)
        seconds = delta.total_seconds()
        if seconds <= 0:
            return "resetting now"
        hours, minutes = divmod(int(seconds) // 60, 60)
        if hours >= 24:
            local = self.resets_at.astimezone()
            return f"resets {local:%a %H:%M}"
        if hours:
            return f"resets in {hours}h {minutes:02d}m"
        return f"resets in {minutes}m"

    def reset_short(self) -> str:
        """The same countdown compressed to at most 4 characters, for the icon.

        Format adapts to magnitude so the text stays as large as the 32px canvas
        allows: "6d" over a day out, "1:32" within the day, "32m" inside the hour,
        "now" when it is about to roll over.
        """
        if self.resets_at is None:
            return ""
        seconds = (self.resets_at - datetime.now(timezone.utc)).total_seconds()
        if seconds < 60:
            return "now"
        minutes = int(seconds) // 60
        if minutes < 60:
            return f"{minutes}m"
        hours, mins = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}:{mins:02d}"
        return f"{hours // 24}d"

    def summary(self) -> str:
        if not self.known:
            return f"{self.label}: unknown"
        reset = self.reset_text()
        tail = f"   {reset}" if reset else ""
        return f"{self.label}: {self.used_percent:.0f}% used{tail}"


@dataclass
class Usage:
    """A parsed snapshot of the whole response."""

    session: Gauge = field(default_factory=lambda: Gauge("Session (5h)"))
    weekly: Gauge = field(default_factory=lambda: Gauge("Weekly"))
    extra: list[Gauge] = field(default_factory=list)
    error: str | None = None
    # Seconds the caller should wait before trying again, when the server told us
    # to back off. None means "use the normal cadence".
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.has_numbers

    @property
    def has_numbers(self) -> bool:
        """Whether there is anything worth displaying, error or not.

        A reading can carry both numbers and an error - a stale snapshot shown
        while rate-limited. Callers check this rather than ``ok`` so the UI keeps
        showing something.
        """
        return self.session.known or self.weekly.known


def _as_percent(value) -> float | None:
    """Coerce a utilization/percent field to a sane 0-100 float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _as_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_usage(payload: dict) -> Usage:
    """Read the response tolerantly.

    Two sources describe the same windows: the top-level `five_hour`/`seven_day`
    objects and the `limits[]` array. We prefer `limits[]` (it carries the
    friendlier integer `percent`) and fall back to the top-level objects. Any
    field that isn't the expected type is skipped.
    """
    usage = Usage()
    if not isinstance(payload, dict):
        usage.error = "Unexpected response shape"
        return usage

    # Preferred: the limits array.
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or entry.get("group") or "")
        target = None
        if kind == "session":
            target = usage.session
        elif kind.startswith("weekly"):
            target = usage.weekly
        if target is None:
            continue
        target.used_percent = _as_percent(entry.get("percent"))
        target.resets_at = _as_time(entry.get("resets_at"))

    # Fallback / fill-in from the top-level objects.
    for key, gauge in (("five_hour", usage.session), ("seven_day", usage.weekly)):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        if gauge.used_percent is None:
            gauge.used_percent = _as_percent(block.get("utilization"))
        if gauge.resets_at is None:
            gauge.resets_at = _as_time(block.get("resets_at"))

    # Model-scoped weekly buckets exist on Max plans; null on Pro. Show if real.
    for key, label in (("seven_day_opus", "Weekly (Opus)"), ("seven_day_sonnet", "Weekly (Sonnet)")):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        percent = _as_percent(block.get("utilization"))
        if percent is None:
            continue
        usage.extra.append(Gauge(label, percent, _as_time(block.get("resets_at"))))

    if not usage.session.known and not usage.weekly.known:
        usage.error = "No usable limits in response"
    return usage


def fetch_usage() -> Usage:
    """One poll. Never raises; failures come back on Usage.error."""
    try:
        token = read_access_token()
    except AuthError as exc:
        return Usage(error=str(exc))

    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # We deliberately do NOT refresh the token ourselves: rotating it
            # could desync Claude Code's own credentials file. Running any
            # Claude Code command refreshes it for us.
            return Usage(error="Token rejected - run any Claude Code command to refresh")
        if exc.code == 429:
            # The endpoint sends "Retry-After: 0", which would mean retry
            # immediately and is exactly the wrong thing to do while limited.
            # Honour the header only when it is a sane positive number.
            delay = None
            try:
                delay = float(exc.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                delay = None
            if not delay or delay <= 0:
                delay = RATE_LIMIT_BACKOFF
            return Usage(error="Rate limited - backing off", retry_after=delay)
        return Usage(error=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return Usage(error=f"Offline or unreachable ({exc.reason})")
    except Exception as exc:  # noqa: BLE001 - the tray must survive anything
        return Usage(error=f"{type(exc).__name__}")

    try:
        payload = json.loads(body)
    except ValueError:
        return Usage(error="Response was not JSON")
    return parse_usage(payload)


def _gauge_to_dict(gauge: Gauge) -> dict:
    return {
        "label": gauge.label,
        "used_percent": gauge.used_percent,
        "resets_at": gauge.resets_at.isoformat() if gauge.resets_at else None,
    }


def _gauge_from_dict(data: dict) -> Gauge:
    return Gauge(
        label=str(data.get("label", "")),
        used_percent=_as_percent(data.get("used_percent")),
        resets_at=_as_time(data.get("resets_at")),
    )


def write_snapshot(usage: Usage) -> None:
    """Publish a good reading for other processes. Failures are ignored: the cache
    is an optimization, never a requirement."""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "session": _gauge_to_dict(usage.session),
            "weekly": _gauge_to_dict(usage.weekly),
            "extra": [_gauge_to_dict(g) for g in usage.extra],
        }
        temp = SNAPSHOT_PATH + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(temp, SNAPSHOT_PATH)  # atomic, so no reader sees a half-write
    except OSError:
        # Non-fatal, but log it: a snapshot that stops updating looks exactly
        # like a dead poll loop.
        log.warning("Could not write snapshot to %s", SNAPSHOT_PATH,
                    exc_info=True)


def read_snapshot(max_age: float) -> Usage | None:
    """A cached reading younger than ``max_age`` seconds, or None."""
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        fetched = _as_time(data.get("fetched_at"))
        if fetched is None:
            return None
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        if age < 0 or age > max_age:
            return None
        usage = Usage(
            session=_gauge_from_dict(data.get("session") or {}),
            weekly=_gauge_from_dict(data.get("weekly") or {}),
            extra=[_gauge_from_dict(g) for g in (data.get("extra") or [])
                   if isinstance(g, dict)],
        )
        return usage if usage.ok else None
    except (OSError, ValueError, TypeError):
        return None


def get_usage(max_age: float = CACHE_FRESH_SECONDS) -> Usage:
    """The reading every UI should call: cache first, network only if stale.

    A restart inside the cache window is free rather than costing an immediate poll.
    """
    cached = read_snapshot(max_age)
    if cached is not None:
        return cached
    usage = fetch_usage()
    if usage.ok:
        write_snapshot(usage)
        return usage
    # Prefer stale numbers plus an error note over blanks - an empty gauge reads
    # as "no data" rather than "offline".
    stale = read_snapshot(max_age=float("inf"))
    if stale is not None:
        stale.error = usage.error
        stale.retry_after = usage.retry_after
        return stale
    return usage


# --- (5) Icon rendering ------------------------------------------------------

def _set_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - already set, or pre-8.1
        pass


def tray_icon_size() -> int:
    """The pixel size Windows wants for a tray icon on this display.

    pystray hands our bitmap straight to Shell_NotifyIcon without resizing, so
    matching this exactly means Windows never rescales the digits. 32px here at
    200% scaling; 16px on an unscaled display.
    """
    try:
        import ctypes

        size = int(ctypes.windll.user32.GetSystemMetrics(49))  # SM_CXSMICON
        return size if 12 <= size <= 256 else 32
    except Exception:  # noqa: BLE001
        return 32


_set_dpi_awareness()

_font_cache: dict[tuple[int, str], object] = {}


def taskbar_theme() -> str:
    """'light' or 'dark', from the same registry value the shell uses.

    Read per render rather than cached, so switching Windows themes recolors the
    icon on the next poll without a restart.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as key:
            return "light" if winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] else "dark"
    except OSError:
        return "dark"


def load_font(size: int, condensed: bool = False):
    """Best available bold face at this pixel size, or PIL's builtin.

    With ``condensed`` set, narrow faces are tried first - Bahnschrift is a
    variable font, so the "Bold Condensed" instance is selected explicitly.
    """
    from PIL import ImageFont

    specs: list[tuple[str, str | None]] = list(CONDENSED_CANDIDATES) if condensed else []
    specs += [(name, None) for name in FONT_CANDIDATES]

    for name, variation in specs:
        # The variation belongs in the key: set_variation_by_name mutates the font
        # object, so two instances of one face must never share a cache entry.
        key = (size, name, variation)
        if key in _font_cache:
            return _font_cache[key]
        try:
            font = ImageFont.truetype(rf"C:\Windows\Fonts\{name}", size)
        except OSError:
            continue
        if variation:
            try:
                font.set_variation_by_name(variation)
            except Exception:  # noqa: BLE001 - static build, or no such instance
                pass
        _font_cache[key] = font
        return font
    return ImageFont.load_default()


def fit_text(draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int,
             condensed: bool = False):
    """Pick the biggest font size whose rendered text fits the box.

    Widths vary a lot between "7", "41", "100" and "1:32", and the boxes are only
    13-26px tall, so measuring beats guessing.
    """
    best = None
    for size in range(box_h + 10, 5, -1):
        font = load_font(size, condensed)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= box_w and bottom - top <= box_h:
            best = (font, left, top, right - left, bottom - top)
            break
    if best is None:
        font = load_font(8, condensed)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        best = (font, left, top, right - left, bottom - top)
    return best


def draw_centered(draw: ImageDraw.ImageDraw, text: str, canvas_w: int, y0: int,
                  box_h: int, color, condensed: bool = False, pad_x: int = 1) -> None:
    """Center text horizontally in the canvas and vertically in its row.

    ``pad_x`` keeps a margin at the sides: without it, wide strings like "12:03"
    fit exactly and end up touching both edges, which reads badly against a busy
    taskbar. The text is still centerd on the full canvas, only the size search is
    constrained.
    """
    font, off_x, off_y, text_w, text_h = fit_text(
        draw, text, canvas_w - 2 * pad_x, box_h, condensed)
    draw.text(
        ((canvas_w - text_w) / 2 - off_x, y0 + (box_h - text_h) / 2 - off_y),
        text,
        font=font,
        fill=color,
    )


def fill_color(used_percent: float, theme: str) -> tuple[int, int, int]:
    palette = PALETTE[theme]
    if used_percent >= CRIT_AT:
        return palette["crit"]
    if used_percent >= WARN_AT:
        return palette["warn"]
    return palette["ok"]


def _draw_bar(draw, box, gauge: Gauge, theme: str, radius: int, outline: bool) -> None:
    palette = PALETTE[theme]
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=radius,
        fill=palette["track"],
        outline=palette["edge"] if outline else None,
        width=1 if outline else 0,
    )
    if not gauge.known:
        # Dim dash, never an empty bar - empty reads as "plenty left".
        mid_y = (y0 + y1) // 2
        inset = max(2, (x1 - x0) // 5)
        draw.line([x0 + inset, mid_y, x1 - inset, mid_y], fill=palette["unknown"],
                  width=max(1, (y1 - y0) // 3))
        return
    pad = 1 if outline else 0
    inner_x0, inner_x1 = x0 + pad, x1 - pad
    span = inner_x1 - inner_x0
    filled = int(round(span * gauge.used_percent / 100.0))
    if filled > 0:
        draw.rounded_rectangle(
            [inner_x0, y0 + pad, inner_x0 + max(filled, 2), y1 - pad],
            radius=max(1, radius - 1),
            fill=fill_color(gauge.used_percent, theme),
        )


def render_icon(usage: Usage, settings: Settings | None = None) -> Image.Image:
    """Draw the tray icon at exactly the size Windows will display.

    Two styles. "number" prints the percentage so it is readable at a glance
    without hovering - the reason this exists - with the other window as a thin
    bar underneath. "bars" is the original two-bar gauge.
    """
    style = settings.icon_style if settings else STYLE_NUMBER
    metric = settings.number_metric if settings else "session"
    theme = taskbar_theme()
    palette = PALETTE[theme]

    size = tray_icon_size()
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if style == STYLE_BARS:
        bar_h = max(5, int(size * 0.30))
        gap = max(2, int(size * 0.10))
        total = bar_h * 2 + gap
        top = (size - total) // 2
        for gauge, y0 in ((usage.session, top), (usage.weekly, top + bar_h + gap)):
            _draw_bar(draw, (0, y0, size - 1, y0 + bar_h), gauge, theme,
                      radius=max(2, bar_h // 3), outline=True)
        return image

    primary = usage.session if metric == "session" else usage.weekly
    secondary = usage.weekly if metric == "session" else usage.session

    if primary.known:
        text = f"{primary.used_percent:.0f}"
        color = fill_color(primary.used_percent, theme)
    else:
        text = "?"
        color = palette["unknown"][:3]

    countdown = primary.reset_short() if primary.known else ""
    # Below ~24px a second row cannot carry a 4-character string. Single number.
    want_time = bool(settings.show_time if settings else True)
    show_time = want_time and size >= 24 and bool(countdown)

    if show_time:
        # 19px number over 12px countdown, 1px gap. Proportional for other DPIs.
        time_h = max(6, int(round(size * 0.375)))
        pct_h = size - time_h - 1
        draw_centered(draw, text, size, 0, pct_h, color, condensed=True)
        draw_centered(draw, countdown, size, pct_h + 1, time_h,
                      palette["muted"], condensed=True)
        return image

    # Single big number, thicker bar for the other window. Condensed here too -
    # the wider face capped this path at 17px.
    strip_h = max(3, int(round(size * 0.13)))
    strip_y0 = size - strip_h
    draw_centered(draw, text, size, 0, strip_y0 - 1, color, condensed=True)
    _draw_bar(draw, (0, strip_y0, size - 1, size - 1), secondary, theme,
              radius=max(1, strip_h // 2), outline=False)
    return image


def tooltip(usage: Usage) -> str:
    if usage.error:
        return f"Claude Limits - {usage.error}"
    parts = [usage.session.summary(), usage.weekly.summary()]
    return "Claude Limits\n" + "\n".join(parts)


# --- (6) Autostart -----------------------------------------------------------


def autostart_command(script: str | None = None) -> str:
    """pythonw so the app starts with no console window attached.

    ``script`` defaults to this file; an explicit path can be passed to register
    a different entry point at login.
    """
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pythonw if os.path.exists(pythonw) else sys.executable
    target = os.path.abspath(script or __file__)
    return f'"{runner}" "{target}"'


def autostart_enabled(name: str = RUN_VALUE_NAME) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool, name: str = RUN_VALUE_NAME,
                  script: str | None = None) -> None:
    try:
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ,
                                  autostart_command(script))
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
    except OSError:
        log.warning("Could not update the autostart registry entry",
                    exc_info=True)


# --- (7) Tray registration ---------------------------------------------------

# Shell_NotifyIcon returns a BOOL that pystray discards without an errcheck, so a
# rejected registration is silent: no exception, no icon, and pystray still thinks
# it is visible - which disables its own WM_TASKBARCREATED recovery. Recording the
# result is what makes a retry possible.
#
# Bounded: the errcheck fires on every call and redraw() makes two per
# REDRAW_INTERVAL, so an unbounded list would grow by ~8,600 entries a day.
_notify_results: collections.deque[tuple[int, bool]] = collections.deque(maxlen=32)
_NIM_ADD: int | None = None


def _install_notify_probe() -> bool:
    """Record the outcome of every Shell_NotifyIcon call. Returns True if the probe
    is in place. Behavior is unchanged: the original result is passed straight
    through, exactly as pystray would have received it."""
    global _NIM_ADD

    def errcheck(result, func, arguments):
        try:
            code = int(arguments[0])
        except (IndexError, TypeError, ValueError):
            code = -1
        _notify_results.append((code, bool(result)))
        return result

    try:
        from pystray._util import win32 as pystray_win32
        _NIM_ADD = int(pystray_win32.NIM_ADD)
        pystray_win32.Shell_NotifyIcon.errcheck = errcheck
        return True
    except Exception:  # noqa: BLE001 - a pystray internal, so treat it as optional
        log.warning("Could not probe Shell_NotifyIcon; icon failures will be silent")
        return False


def _add_succeeded() -> bool | None:
    """Did the NIM_ADD land? None when the probe saw no add at all.

    Only the add is worth judging. Setting `visible` also triggers a NIM_MODIFY
    via pystray's `_update_icon()`, and on the first call that modify runs before
    the icon exists and so fails by design - counting it would report a refusal
    for every healthy startup.
    """
    adds = [ok for code, ok in _notify_results if code == _NIM_ADD]
    if not adds:
        return None
    return adds[-1]


def retry_delay(attempt: int, base: float = 2.0, cap: float = 30.0) -> float:
    """Doubling backoff, holding each step for two attempts: 2 2 4 4 8 8 16 16 30."""
    return min(base * 2 ** ((attempt - 1) // 2), cap)


def show_icon(icon: pystray.Icon, attempts: int = 10) -> bool:
    """Register the tray icon, retrying while the notification area settles.

    Started from the Run key, this can run before the notification area accepts
    icons, and a refused registration is never retried by anything else. Each
    retry clears the visible flag first so the next attempt is a real NIM_ADD.
    Backs off to roughly 90 seconds total; polling waits on this.
    """
    probed = _install_notify_probe()
    for attempt in range(1, attempts + 1):
        _notify_results.clear()
        try:
            icon.visible = False
            icon.visible = True
        except Exception:  # noqa: BLE001
            log.exception("Tray icon registration raised (attempt %d/%d)",
                          attempt, attempts)
        else:
            added = _add_succeeded()
            # With no probe, or no add observed, there is nothing to judge:
            # trust the call rather than tear down a possibly healthy icon.
            if not probed or added is not False:
                log.info("Tray icon registered (attempt %d)", attempt)
                return True
            log.warning("Notification area refused the icon (attempt %d/%d)",
                        attempt, attempts)
        if attempt < attempts:
            time.sleep(retry_delay(attempt))
    log.error("Gave up registering the tray icon after %d attempts; "
              "polling continues without an icon", attempts)
    return False


# --- (8) Single instance -----------------------------------------------------

# Held for the life of the process. Windows frees a mutex when its last handle
# closes, so this must not be garbage-collected - and equally, a crash cannot
# leave a stale lock behind the way a lockfile can.
_instance_mutex = None


def claim_single_instance(name: str = MUTEX_NAME) -> bool:
    """True if this process is the only instance; False if one is already running.

    The Run-key entry starts a copy at login, so a manual launch on top would give
    two tray icons writing the same files and polling the same rate-limited
    endpoint. Existence of the named object is the whole signal.

    A failed check returns True: a broken guard must never keep the tray hidden.
    """
    global _instance_mutex
    error_already_exists = 183
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not handle:
            log.warning("Single-instance mutex could not be created (error %d); "
                        "starting anyway", err)
            return True
        if err == error_already_exists:
            # CreateMutexW still hands back a handle to the existing object.
            kernel32.CloseHandle(handle)
            return False
        _instance_mutex = handle
        return True
    except Exception:  # noqa: BLE001
        log.exception("Single-instance check failed; starting anyway")
        return True


# --- (9) The app -------------------------------------------------------------


class ClaudeLimitsApp:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.usage = Usage()
        self.consecutive_failures = 0
        self.last_polled: datetime | None = None
        self.notified_below = False
        self.force_poll = False
        self.retry_after: float | None = None
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.icon = pystray.Icon(
            "claude_limits",
            icon=render_icon(self.usage, self.settings),
            title="Claude Limits - starting...",
            menu=self.build_menu(),
        )

    # --- menu ---

    def build_menu(self) -> pystray.Menu:
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda _: self.usage.session.summary() if not self.usage.error
                 else f"! {self.usage.error}", None, enabled=False),
            item(lambda _: self.usage.weekly.summary() if not self.usage.error
                 else "", None, enabled=False,
                 visible=lambda _: not self.usage.error),
            pystray.Menu.SEPARATOR,
            item(lambda _: self.extra_text(), None, enabled=False,
                 visible=lambda _: bool(self.usage.extra)),
            item(lambda _: self.freshness_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Refresh now", self.on_refresh),
            item("Icon shows", pystray.Menu(
                item("Session number + weekly bar",
                     lambda: self.set_display(STYLE_NUMBER, "session"),
                     radio=True,
                     checked=lambda _: self.settings.icon_style == STYLE_NUMBER
                     and self.settings.number_metric == "session"),
                item("Weekly number + session bar",
                     lambda: self.set_display(STYLE_NUMBER, "weekly"),
                     radio=True,
                     checked=lambda _: self.settings.icon_style == STYLE_NUMBER
                     and self.settings.number_metric == "weekly"),
                item("Two bars, no number",
                     lambda: self.set_display(STYLE_BARS, self.settings.number_metric),
                     radio=True,
                     checked=lambda _: self.settings.icon_style == STYLE_BARS),
                pystray.Menu.SEPARATOR,
                item("Show time to reset",
                     self.on_toggle_time,
                     checked=lambda _: self.settings.show_time,
                     enabled=lambda _: self.settings.icon_style == STYLE_NUMBER),
            )),
            item("Notify when weekly is low",
                 self.on_toggle_notify,
                 checked=lambda _: self.settings.notify_enabled),
            item("Start with Windows",
                 self.on_toggle_autostart,
                 checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item("Quit", self.on_quit),
        )

    def extra_text(self) -> str:
        return "   ".join(g.summary() for g in self.usage.extra)

    def freshness_text(self) -> str:
        if self.last_polled is None:
            return "not polled yet"
        age = int((datetime.now(timezone.utc) - self.last_polled).total_seconds())
        when = "just now" if age < 60 else f"{age // 60}m ago"
        if self.consecutive_failures:
            return f"updated {when} - {self.consecutive_failures} failed since"
        return f"updated {when}"

    # --- actions ---

    def on_refresh(self, _icon=None, _item=None) -> None:
        # The flag matters: the loop now wakes for plain repaints too, so without
        # it "Refresh now" could land on a repaint instead of a real poll.
        self.consecutive_failures = 0
        self.force_poll = True
        self.wake.set()

    def set_display(self, style: str, metric: str) -> None:
        """Switch icon style/metric and redraw immediately, without waiting for
        the next poll."""
        self.settings.icon_style = style
        self.settings.number_metric = metric
        self.settings.save()
        self.redraw()
        self.icon.update_menu()

    def on_toggle_time(self, _icon=None, _item=None) -> None:
        """Add or remove the countdown row. Unticking restores the previous
        single-number layout exactly - it gates only the rendering."""
        self.settings.show_time = not self.settings.show_time
        self.settings.save()
        self.redraw()
        self.icon.update_menu()

    def on_toggle_notify(self, _icon=None, _item=None) -> None:
        self.settings.notify_enabled = not self.settings.notify_enabled
        self.settings.save()
        self.icon.update_menu()

    def on_toggle_autostart(self, _icon=None, _item=None) -> None:
        set_autostart(not autostart_enabled())
        self.icon.update_menu()

    def on_quit(self, _icon=None, _item=None) -> None:
        self.stopping.set()
        self.wake.set()
        self.icon.stop()

    # --- polling ---

    def poll_interval(self) -> int:
        for threshold, interval in BACKOFF_LADDER:
            if self.consecutive_failures < threshold:
                return interval if self.consecutive_failures else BASE_INTERVAL
        return MAX_INTERVAL

    def maybe_notify(self) -> None:
        """One toast when weekly headroom crosses below the threshold.

        The latch clears once remaining climbs back to/above the threshold, so a
        new week re-arms it. pystray gives no delivery confirmation, so a toast
        suppressed by Focus Assist is missed rather than retried.
        """
        if not self.settings.notify_enabled or not self.usage.weekly.known:
            return
        remaining = 100.0 - self.usage.weekly.used_percent
        if remaining < self.settings.notify_threshold:
            if not self.notified_below:
                self.notified_below = True
                try:
                    self.icon.notify(
                        f"{remaining:.0f}% of your weekly Claude quota is left "
                        f"({self.usage.weekly.reset_text()}).",
                        "Claude weekly limit is running low",
                    )
                except Exception:  # noqa: BLE001
                    self.notified_below = False
        else:
            self.notified_below = False

    def redraw(self) -> None:
        """Repaint the icon and tooltip from the snapshot we already have.

        No network. This is what makes the countdown tick between polls.
        """
        self.icon.icon = render_icon(self.usage, self.settings)
        self.icon.title = tooltip(self.usage)

    def apply(self, usage: Usage) -> None:
        if usage.has_numbers:
            # Adopt the reading even when it carries an error, so a stale snapshot
            # returned during a rate limit still populates the icon.
            self.usage = usage
        else:
            # Nothing to show: keep whatever is on screen, change only the error.
            self.usage.error = usage.error
        if usage.ok:
            self.consecutive_failures = 0
            self.last_polled = datetime.now(timezone.utc)
            self.maybe_notify()
        else:
            self.consecutive_failures += 1
        # A server-dictated wait overrides our own cadence for the next round.
        self.retry_after = usage.retry_after
        self.redraw()
        self.icon.update_menu()

    def loop(self, icon: pystray.Icon) -> None:
        """Two cadences in one loop: poll on the backoff schedule, repaint often.

        The countdown would otherwise sit stale for a whole poll interval, showing
        1:32 for two minutes. Repainting is a 32px render with no request, so it
        can run far more often than the poll.
        """
        show_icon(icon)
        next_poll = 0.0
        while not self.stopping.is_set():
            try:
                next_poll = self.tick(next_poll)
            except Exception:  # noqa: BLE001
                # pystray runs this on a setup thread with no exception handling,
                # so anything escaping kills polling while the icon stays up -
                # indistinguishable from a hang. Absorb, back off, keep going.
                self.consecutive_failures += 1
                log.exception("Poll cycle failed (consecutive failure %d)",
                              self.consecutive_failures)
                next_poll = time.monotonic() + self.poll_interval()
            if self.stopping.is_set():
                break
            wait = min(REDRAW_INTERVAL, max(1.0, next_poll - time.monotonic()))
            self.wake.wait(wait)
            self.wake.clear()

    def tick(self, next_poll: float) -> float:
        """One pass: poll if due, otherwise just repaint. Returns the next due time."""
        if self.force_poll or time.monotonic() >= next_poll:
            forced = self.force_poll
            self.force_poll = False
            # A forced refresh must bypass the cache, or "Refresh now" is a no-op
            # while the snapshot is still fresh. max_age=0 always re-fetches.
            usage = get_usage(0 if forced else CACHE_FRESH_SECONDS)
            if usage.ok:
                usage.error = None
            self.apply(usage)
            if usage.ok:
                log.info("Poll ok: session %s%%, weekly %s%%",
                         usage.session.used_percent, usage.weekly.used_percent)
            else:
                log.warning("Poll failed: %s", usage.error)
            return time.monotonic() + (self.retry_after or self.poll_interval())
        self.redraw()
        return next_poll

    def run(self) -> None:
        self.icon.run(setup=self.loop)


def main() -> int:
    setup_logging()
    log.info("Starting claude-limits (pid %d, python %s)",
             os.getpid(), sys.version.split()[0])
    if not claim_single_instance():
        log.info("Another instance is already running; exiting.")
        return 0
    try:
        ClaudeLimitsApp().run()
    except Exception:
        log.exception("Fatal error; the tray icon is exiting")
        raise
    log.info("Stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
