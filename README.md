# Claude Limits

A Windows system-tray gauge for your Claude usage limits. The session percentage **and** the reset countdown are drawn inside the tray icon itself: always visible, no hover, no click, the same way Windows shows battery percentage.

![Claude Limits in the Windows taskbar: a red 99 over a 17m countdown, left of the ENG US language indicator](pictures/tray.png)

*The red `99` over `17m` is Claude Limits, just left of the ENG US language indicator: 99% of the session window used, 17 minutes until it resets.*

## Other tools in this space

Several projects do this, and most are more complete than this one:

| | Claude Limits | [usage-monitor-for-claude](https://github.com/jens-duttke/usage-monitor-for-claude) | [claude-usage-widget](https://github.com/projectvelox/claude-usage-widget) | [Claude-Code-Usage-Monitor](https://github.com/CodeZeno/Claude-Code-Usage-Monitor) |
|---|---|---|---|---|
| Percentage drawn inside the tray icon | ✅ | ✅ | — | — |
| **Countdown drawn inside the tray icon** | ✅ | — | — | — |
| Portable EXE, no Python needed | — | ✅ | ✅ | ✅ |
| Multi-account | — | ✅ | — | — |
| History graph | — | — | ✅ | — |
| Multiple providers | — | — | — | ✅ |

Verified September 2026; these projects ship fast, so check for yourself if it matters to you. All three alternatives show these numbers somewhere: claude-usage-widget in a floating window with a graphical tray icon, Claude-Code-Usage-Monitor in a separate taskbar widget. The rows above are only about whether the figures are rendered inside the tray icon itself.

If you want the most featureful option, use **usage-monitor-for-claude**. This one exists because I wanted both numbers legible in the taskbar without interacting with anything, and nothing else renders the countdown in the icon.

Built with [Claude Code](https://claude.com/claude-code). I specified the behavior and iterated on the design; Claude wrote most of the implementation. Design decisions and the reasoning behind them are in [`.claude/CLAUDE.md`](.claude/CLAUDE.md).

## Requirements

Windows, and Python 3.10+.

```
pip install -r requirements.txt
```

That is `pystray` and `Pillow`; everything else is standard library.

You also need Claude Code installed and logged in, because the app reads the OAuth token it already keeps at `~/.claude/.credentials.json` and never stores one of its own. With no signed-in Claude Code the tray shows `?` and says so in the menu, rather than failing silently.

## Run it

```
start-claude-limits.cmd
```

Or tick **Start with Windows** in the tray menu, which writes a per-user `Run` registry entry, no admin needed.

On Windows 11, the icon lands in the hidden-icons overflow (click the `^` icon to see it) by default; drag it onto the visible taskbar once and it stays.

Only one copy runs at a time. A second launch sees the first, writes a line to the log and exits, so a manual launch on top of the one started at login does not give you two icons and two pollers.

## What you see

```
 41     session % used   (green <60, amber 60-85, red above)
1:32    time until it resets
```

The countdown never exceeds four characters and always rounds down, so the icon never claims more time than you have:

| Remaining | Shown |
|---|---|
| 24h or more | `6d` |
| 1h or more | `1:32` |
| under 1h | `32m` |
| under 1m | `now` |

**Icon shows ▸** in the menu switches between:

| Option | |
|---|---|
| Session number + weekly bar | default |
| Weekly number + session bar | if the weekly ceiling is what you care about |
| Two bars, no number | the original gauge |
| Show time to reset | untick to drop the countdown row |

Colors follow your light or dark taskbar and update on the next poll, no restart. Weekly is always in the tooltip and the menu, so it does not need its own row in a 32px icon.

## What the numbers cover

Account-wide, not just Claude Code: claude.ai, the desktop and mobile apps, and the CLI all draw on one pool on Pro/Max. On Max plans the model-scoped `Weekly (Opus)` and `Weekly (Sonnet)` buckets appear in the menu too; they are null on Pro, so they stay hidden (I have not tested this on a Max plan though).

## How it authenticates

It doesn't - not on its own. Every poll re-reads the access token Claude Code already maintains in `~/.claude/.credentials.json`, so there is no login flow, no cookie capture, and no secret of its own on disk. The token is never logged or written anywhere.

If the token is rejected, then the app says so rather than refreshing it itself, since rotating it could desync Claude Code's own credentials file. Running any Claude Code command refreshes it.

## Endpoint and polling

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <accessToken>
anthropic-beta: oauth-2025-04-20
```

**Undocumented and internal.** Anthropic can change the response without notice, so every field is read defensively: wrong types, missing keys and unknown keys are skipped rather than raising.

Polls every 180s, backing off to 300s, 600s then 1800s after consecutive failures. The endpoint rate-limits hard and returns `Retry-After: 0`, which is no guidance at all, so a 429 backs off 900s instead. The icon repaints every 20s from memory so the countdown ticks without a request, and **Refresh now** forces a real one.

On any failure the last known numbers stay on screen with the error as a footnote. Blanking the display would read as "plenty left", which is the wrong signal when the poll is broken.

## Settings and log

Both live in `%APPDATA%\ClaudeLimits\`.

`settings.json` holds your menu choices, plus **Notify when weekly is low**: one toast when weekly *remaining* drops below 20%, re-arming once it climbs back above. Not a fan of these myself, so it's off by default, but feel free to turn it on if you are.

`claude-limits.log` records startup, tray registration, every poll outcome and every exception. Never the token. It's capped at 256 KB and keeps two older copies, so it never grows past about 750 KB - roughly a month of polls. It exists because the app runs under `pythonw.exe`, which has no console and discards stderr, so without it a failure leaves no trace at all.

| Line | Meaning |
|---|---|
| `Tray icon registered (attempt 1)` | normal startup |
| `Notification area refused the icon (attempt N/10)` | Windows rejected it; retried with backoff over ~90s |
| `Gave up registering the tray icon` | no icon this session, but polling continues |
| `Poll cycle failed (consecutive failure N)` | one poll raised; the loop backed off and carried on |
| `Another instance is already running; exiting.` | you launched a second copy; the first is unaffected |

## Files

| | |
|---|---|
| `claude_limits.py` | the whole app |
| `start-claude-limits.cmd` | launches it via `pythonw` (no console window) |
| `requirements.txt` | `pystray` and `Pillow` |
| `.claude/CLAUDE.md` | design decisions and the reasoning behind them |
| `LICENSE` | MIT |

## Disclaimer

Claude Limits is an independent project, not affiliated with or endorsed by Anthropic. "Claude" and "Anthropic" are trademarks of Anthropic, PBC, used here only to describe what the tool works with.
