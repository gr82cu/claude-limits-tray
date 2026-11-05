"""Claude Code status line that feeds Claude Limits for free.

Claude Code pipes a JSON blob to its status line command after each turn, and
that blob carries the same five-hour and weekly figures the tray polls for,
taken from the headers of requests Claude Code was making anyway. Writing them
into the tray's snapshot means the tray skips its own poll while Claude Code is
busy - which is exactly when the shared /api/oauth/usage budget runs out.

Only the percentages, reset times and context size are read, and only the
percentages and reset times are saved. Nothing else from the blob (session id,
paths, model) is written anywhere. The context size is only displayed.

Usage, in ~/.claude/settings.json:

    "statusLine": {"type": "command",
                   "command": "python C:/path/to/statusline.py"}

To keep an existing status line, put its command after "--"; it gets the same
input and its output is printed instead of ours:

    "command": "python C:/path/to/statusline.py -- your-existing-command"
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ClaudeLimits")
SNAPSHOT_PATH = os.path.join(SETTINGS_DIR, "snapshot.json")


def _percent(block: dict) -> float | None:
    value = block.get("used_percentage")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _reset(block: dict) -> str | None:
    """resets_at as ISO 8601. Claude Code sends epoch seconds; accept ISO too."""
    value = block.get("resets_at")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


def write_snapshot(limits: dict) -> tuple[float | None, float | None]:
    """Write the snapshot in the tray's format and return (session, weekly)."""
    gauges = {}
    for key, name, label in (("five_hour", "session", "Session (5h)"),
                             ("seven_day", "weekly", "Weekly")):
        block = limits.get(key)
        if isinstance(block, dict):
            gauges[name] = {"label": label, "used_percent": _percent(block),
                            "resets_at": _reset(block)}
    session = gauges.get("session", {}).get("used_percent")
    weekly = gauges.get("weekly", {}).get("used_percent")
    if session is None and weekly is None:
        return None, None
    # The status line has no model-scoped buckets, so keep the last ones the tray
    # polled rather than blanking them; the tray refreshes them once idle.
    extra = []
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
            previous = json.load(fh)
        extra = previous.get("extra") or []
        gauges.setdefault("session", previous.get("session"))
        gauges.setdefault("weekly", previous.get("weekly"))
    except (OSError, ValueError, AttributeError):
        pass
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "statusline",
        "session": gauges.get("session"),
        "weekly": gauges.get("weekly"),
        "extra": extra,
    }
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        temp = SNAPSHOT_PATH + ".statusline.tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(temp, SNAPSHOT_PATH)  # atomic, so the tray never reads half a file
    except OSError:
        pass
    return session, weekly


def context_tokens(data: dict) -> float | None:
    """Tokens in the context now, from the last request's usage if it's there."""
    window = data.get("context_window")
    if not isinstance(window, dict):
        return None
    usage = window.get("current_usage")
    if isinstance(usage, dict):
        counts = [usage.get(key) for key in ("input_tokens",
                                             "cache_creation_input_tokens",
                                             "cache_read_input_tokens")]
        counts = [n for n in counts
                  if isinstance(n, (int, float)) and not isinstance(n, bool)]
        if counts:
            return float(sum(counts))
    percent, size = _percent(window), window.get("context_window_size")
    if percent is None or isinstance(size, bool) or not isinstance(size, (int, float)):
        return None
    return percent / 100 * size


def main() -> int:
    # Claude Code reads UTF-8; the Windows console default would mangle the dot.
    sys.stdout.reconfigure(encoding="utf-8")
    raw = sys.stdin.read()
    session = weekly = context = None
    try:
        data = json.loads(raw)
        limits = data.get("rate_limits") if isinstance(data, dict) else None
        if isinstance(limits, dict):
            session, weekly = write_snapshot(limits)
        if isinstance(data, dict):
            context = context_tokens(data)
    except ValueError:
        pass

    if "--" in sys.argv:
        command = sys.argv[sys.argv.index("--") + 1:]
        if command:
            # One argument is a whole shell command line; several are argv.
            if len(command) == 1:
                command = command[0]
            result = subprocess.run(command, input=raw, text=True,
                                    capture_output=True,
                                    shell=isinstance(command, str))
            sys.stdout.write(result.stdout)
            return 0

    parts = []
    if session is not None:
        parts.append(f"5h {session:.0f}%")
    if weekly is not None:
        parts.append(f"wk {weekly:.0f}%")
    if context is not None:
        parts.append(f"ctx {context / 1000:.0f}k")
    print(" · ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
