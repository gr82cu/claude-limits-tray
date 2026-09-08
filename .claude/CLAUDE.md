# Project Guidelines

Apply Python best practices and clean code principles. Only change code relevant to the prompt. Prioritize readability and auditability: this app reads the user's Claude credentials, and a reader must be able to verify at a glance that it does nothing else with them.

Every rule below came out of a measurement or a failure. Where a rule says "do not revert", something was already tried and rejected.

## Scope

- **Tray icon only.** A tkinter desktop widget was built and deleted the same day: it was the wrong surface and it introduced a second poller. Do not rebuild it or propose one.
- **One poller, always.** Two independent pollers is what tripped the endpoint's rate limit. Any new feature reads the existing in-memory snapshot rather than fetching.
- **One process, always.** `claim_single_instance()` claims the named mutex `Local\ClaudeLimits` at the top of `main()` and a second copy exits immediately. The Run key starts one at login, so a manual launch on top of it is the common case, not an edge case - it previously produced two icons, two pollers, and two writers of the same settings, snapshot and log file. Existence of the named object is the entire signal: nothing is acquired and nothing needs releasing, and Windows frees it on process death so a crash cannot leave a stale lock. **A failure of the check must always return True** - a broken guard must never be the reason the tray does not appear.

## Platform

- Windows-only. No `sys.platform` guards; `winreg` and `ctypes.windll` are used unconditionally.

## Tray icon rendering

- **Everything is drawn INSIDE the icon.** Windows gives third-party tray icons no way to draw text beside them: the shell draws the system battery's "54%" itself, which is not an API available to applications. Do not attempt a side label.
- Rendered at exactly `GetSystemMetrics(SM_CXSMICON)` (32px on a 200%-scaled display). pystray passes the bitmap through unresized, so matching that size is what keeps the digits crisp. Never render at a fixed size and let Windows rescale.
- **Two rows: 19px percentage over 12px countdown, 1px gap.** Three layouts were tried: 18/12, 22/9 and 19/12. 22/9 made the percentage dominate and squeezed the countdown to the edge of legibility; 19/12 is the settled balance. Do not revert to either.
- **Bahnschrift Bold Condensed**, selected via `ImageFont.set_variation_by_name` and verified to actually apply: `41` renders 19px wide against 24px in Segoe UI Bold. That recovered width is the only reason two rows fit at all. Fallback is `arialnb.ttf`. Impact and Arial Narrow Bold both measured worse.
- A 4-character string in a 32px icon is **width**-limited, capped near 11px tall even given the whole canvas. This is why a dedicated countdown row costs nothing, and why cycling between percentage and countdown would gain nothing.
- **No weekly indicator in the icon.** A strip plus its gap costs the percentage 4px, a bad trade when weekly is one hover away. Weekly lives in the tooltip and the menu.
- Three digits (`100`) are width-limited to 19px whatever the layout, which happens to match the two-row size exactly, so nothing changes at 100%.
- The countdown adapts to stay within 4 characters: `6d` / `1:32` / `32m` / `now`. It always rounds **down**, matching the tooltip exactly. The icon must never claim more time than the tooltip shows.
- Number-only layout (countdown toggled off) is 25px. A bug once left this path on non-condensed Segoe UI Bold, which made turning the countdown *off* shrink the number. Any change to one layout must be checked against the other.
- Palette switches on `SystemUsesLightTheme`. A single palette that reads on a light taskbar is muddy on a dark one.

## Polling & rate limits

- `/api/oauth/usage` is **undocumented**. Parse tolerantly and ignore unknown keys: the response carries null fields with internal codenames (`amber_ladder`, `nimbus_quill`). Every read in `parse_usage()` is `isinstance`-guarded and skips anything of an unexpected type; keep it that way.
- Two sources describe the same windows: the `limits[]` array and the top-level `five_hour`/`seven_day` objects. `limits[]` is preferred (it carries a friendlier integer `percent`); the top-level objects fill in whatever is still missing. Do not reverse that order.
- **Known gap: the model only holds two gauges, `session` and `weekly`.** The `limits[]` path is partly generic (`kind == "session"`, `kind.startswith("weekly")`), but the fallback pairs and the model-scoped section hardcode `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet` (lines ~341 and ~351). Quota types added since (Fable, Cowork) are either dropped or collapsed into `weekly`. Deriving the gauges from the response shape instead is a wanted change, not a rule the code currently follows.
- Quota fields can be `null` for plans that do not have them (`seven_day_opus` and `seven_day_sonnet` are null on Pro). Always `(data.get(k) or {})`, never `data.get(k, {})`; the latter returns `None` when the key exists with a null value.
- The endpoint **rate-limits hard and returns a useless `Retry-After: 0`.** Treat a zero or absent header as "no guidance" and fall back to the backoff ladder. Never sleep on a literal 0.
- Poll and repaint are deliberately split: poll on the interval with backoff, repaint every 20s from the in-memory snapshot with no request. Without the split the minutes sit stale for a whole interval. "Refresh now" sets `force_poll`.
- Do not add a second request during a backoff window: a failed secondary call feeds the condition that caused the backoff.

## Logging & failure handling

- **Never let an exception escape `loop()`.** pystray runs it on a setup thread with no exception handling of its own, so anything that escapes kills polling for the life of the process while the message loop and the icon stay up - from outside, indistinguishable from a hang. The body lives in `tick()`; `loop()` catches, logs with a traceback, counts a failure and backs off. This is exactly how the 2026-08-25 failure presented: process alive, tray empty, no poll since the previous evening. Do not simplify the guard away.
- **Never swallow an error silently.** `write_snapshot()`, `Settings.save()` and `set_autostart()` stay non-fatal but log with `exc_info=True` instead of a bare `pass`. A snapshot that quietly stops updating looks like a dead poll loop from outside, and that misdiagnosis has already cost a session.
- The log is `%APPDATA%\ClaudeLimits\claude-limits.log`, rotating at 256KB with two backups. `setup_logging()` must never raise - no log is bad, no tray icon is worse. **Never log the access token**, and never log a whole response object that might one day carry one.
- **Tray registration is retried, never assumed.** `Shell_NotifyIcon` returns a BOOL that pystray discards and installs no `errcheck` on, so a refused registration is completely silent: no exception, no icon, and pystray still believes it is visible, which disables its own `WM_TASKBARCREATED` recovery. The app installs its own `errcheck` solely to observe that flag and passes the result through unchanged.
- **Judge only the `NIM_ADD` result.** Setting `visible` also fires a `NIM_MODIFY` through `_update_icon()`, and on the first call that modify runs before the icon exists and so fails by design. Counting every call reports a refusal for every healthy startup and needlessly deletes and re-adds a working icon - that bug was written and caught the same day, 2026-08-25.
- **When the add is unobservable, treat it as success.** If the probe could not be installed, or the deque has evicted the add, `_add_succeeded()` returns `None` and registration is trusted. Never tear down a possibly healthy icon on absence of evidence.
- **`_notify_results` must stay bounded.** The errcheck fires on every `Shell_NotifyIcon` call and `redraw()` triggers two of them per `REDRAW_INTERVAL`, so an unbounded list grows by ~8,600 entries a day, roughly 15MB a month against a ~40MB baseline. `deque(maxlen=32)`; only the last attempt is ever read.
- The registration retry window is ~90s of doubling backoff (2 2 4 4 8 8 16 16 30) and **polling deliberately waits on it**. A fixed handful of two-second tries only covers a fast login, which was never the broken case; a slow one is what leaves the tray empty for the rest of the session.
- **Known gap: a cache hit logs as `Poll ok`.** A startup within `CACHE_FRESH_SECONDS` reads the shared snapshot rather than the network, and `get_usage()` does not report which it did. Distinguishing them is a wanted change, not a rule the code currently follows.

## Credentials

- **Never hold a token.** Re-read `~/.claude/.credentials.json` on every poll. Claude Code refreshes that file itself, so the app never needs its own secret and must never write one anywhere.
- Walk the credential structure rather than hardcoding `claudeAiOauth.accessToken`, so a shape change does not break auth outright.

## Autostart

- The `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry is named `ClaudeLimits` and is written by the app, not by an installer.
- **Known gap, only reachable once frozen: `autostart_command()` does not handle PyInstaller.** Running as a script it is correct: `sys.executable` is `pythonw.exe` and `__file__` is the real source path. Under PyInstaller `sys.executable` becomes the EXE and `__file__` points inside the `_MEI...` temp extraction directory, which is deleted on exit and renamed every launch, so enabling autostart would write a dead path with no error. Fix before any EXE ships: `if getattr(sys, "frozen", False): return f'"{sys.executable}"'`.

## Build

*(Not yet implemented. This is the spec to build to.)*

- PyInstaller `--onefile --noconsole`, plus `--hidden-import pystray._win32`: the dependency scanner routinely misses pystray's platform backend.
- Bahnschrift and `arialnb.ttf` load from `C:\Windows\Fonts` at runtime, so nothing to bundle.
- Unsigned one-file EXEs trigger SmartScreen on first run. Document it rather than trying to work around it.
