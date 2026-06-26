# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only tool that auto-books seats in the HDU library system (`hdu.huitu.zhishulib.com`). The user logs into the library in Chrome/Edge once; the tool reads the browser's session cookie automatically and submits a daily seat reservation at a scheduled time. Everything is driven from a local web console — users never edit `config.yaml` by hand.

User-facing docs (`README.md`) are in Chinese; match that language for code comments and any user-visible strings.

## Running

```bash
python web_app.py                 # start web console on http://127.0.0.1:8765
python web_app.py --port 9000 --open   # custom port, auto-open browser
pip install -r requirements.txt   # deps: PyYAML, requests, cryptography
```

- `start_web.bat` is the user entry point: finds a working Python (skipping Microsoft Store stubs), checks deps, pre-scans the port, then runs `web_app.py` in the foreground.
- `setup_autostart.bat` / `remove_autostart.bat` register/unregister a `schtasks` on-logon task running `pythonw web_app.py` headless.
- There is no test suite, linter, or build step. Verify changes by running `web_app.py` and exercising the flow, or by importing modules and calling functions directly.

## Architecture

Four Python modules, layered. `web_app.py` and `scheduler.py` both call into `instant_book.py`; cookie extraction is isolated in `cookie_browser.py` + a subprocess probe.

**`instant_book.py` — core booking engine.** `run_booking(...)` is the single entry point both the web UI and scheduler use. The flow: load cookie → `resolve_user()` (discover the huitu internal `uid`, which is *not* the student number) → `query_room_items` → `query_room_detail` → `query_seat_map` → `find_seat` → `wait_until(execute_time)` → `book()` with retries. Key details:
- The booking `plan` is a colon string `roomType:floorId:seatNum:startHour:durationHours` (roomType is always `1` = study room). `parse_plan` / `plan_from_payload` convert to/from this.
- `book()` signs each request with an `Api-Token` header: an MD5 of a canonical, alphabetically-ordered query string, then base64-encoded. The field ordering in `token_source` must exactly match the server's expectation — do not reorder it.
- Retry logic only re-submits when the response message contains `超出可预约座位时间范围` ("time out of range", meaning the booking window hasn't opened yet). All other failures abort immediately. See `booking_result_failed` / `is_time_out_of_range` / `validate_booking_result`.
- `query_seat_map` tries several candidate lookup times (target slot, target+1h, 08:00, current available) because the seat-map endpoint rejects times outside the current bookable window; it merges results across floors until the target floor is found.
- `resolve_user` / `_find_user_info` recursively walk arbitrary JSON (and JSON-in-string values) to heuristically extract `uid`/`name`, scoring candidates by key-name hints.
- The huitu API responses parse with brittle fixed index paths (e.g. `data["content"]["children"][1]["defaultItems"]`, `data["allContent"]["children"][2]["children"]["children"]`). These reflect the live API shape — if the upstream site changes, these break first.

**Cookie acquisition — three layers, in priority order.** The library uses session cookies that the tool steals from a local browser rather than logging in itself (HDU login is CAS `sso.hdu.edu.cn`, account+password but with JS-encrypted password and captcha-on-retry, so automated login is intentionally not attempted). `find_hdu_cookie(preferred_browser)` in `cookie_browser.py` is the entry point and tries:

1. **Dedicated profile via CDP (`cdp_cookie.py`) — the primary path.** The tool maintains its own browser `--user-data-dir` at `browser_profile/`, isolated from the user's everyday browser. The user logs into the library there once (via the "登录图书馆" button → `open_login_window`, a *visible* window). Reads then launch that profile *headless* with `--remote-debugging-port` and pull plaintext cookies over the DevTools Protocol (`Storage.getCookies`). Because the profile is tool-owned, starting/stopping it never disturbs the user's browser. `cdp_cookie.py` includes a minimal stdlib WebSocket client (no third-party deps). The browser family is recorded in `browser_profile/.browser` and reused (v20 keys are browser-bound).
2. **Offline DB decryption (legacy `v10`/`v11`).** Falls back to scanning the user's real Chrome/Edge/Chromium profiles (all `Default` + `Profile *` dirs): copies the locked SQLite `Cookies` DB to temp, decrypts the AES key from `Local State` via Windows DPAPI (`ctypes` + `crypt32`), AES-256-GCM-decrypts each value.
3. **Cached file.** Every successful read is persisted to `browser_cookie.json` (via `safe_cookie.save_cookie_file`), referenced by `config.yaml`'s `auth.cookie_file` as a last-resort fallback.

**Critical CDP constraints (verified empirically, do not regress):** (a) modern Chrome 127+/Edge 127+ encrypt cookies with **App-Bound Encryption — value prefix `v20`** — whose key is SYSTEM-DPAPI-protected and cannot be decrypted offline, which is *why* the CDP path exists; (b) CDP must run against the **real** user-data-dir — copying a profile breaks v20 decryption and returns zero cookies; (c) use **`Storage.getCookies`** (browser target), not `Network.getAllCookies` which returns empty; (d) the target browser must be fully exited before launching with a debug port (a running instance blocks it) — for the user's everyday browser the legacy path's `kill_browser` handles this, but the dedicated profile sidesteps the whole problem.

**Windows DPAPI gotcha (`_dpapi_decrypt`):** the `_DATA_BLOB.pbData` field must be `POINTER(c_char)` (not `c_char_p`, which truncates binary keys at the first NUL), and `CryptUnprotectData`/`LocalFree` must have explicit `argtypes`/`restype` or 64-bit pointers get truncated → `STATUS_HEAP_CORRUPTION` (0xC0000374). Don't revert these.

**`config.yaml` — single source of truth.** Holds auth cookie, user_info, booking plan, session headers/URLs, and automation flags. `ensure_config` auto-generates it from the `_DEFAULT_CONFIG_YAML` template on first run; the template and the committed `config.yaml` must be kept in sync. The web UI edits it via `write_booking_values`, which does *surgical line-level rewriting* of the `booking:` section (preserving comments and other keys) rather than dumping YAML — this is intentional to keep the file human-readable. `session.verify: false` and `trust_env: false` are deliberate (self-signed/proxied environment). `auth.cookie_file` defaults to `browser_cookie.json`; `_load_cookie_file` returns `False` (not raises) when it's missing so first-run booking still works.

**`safe_cookie.py` + `_cookie_probe.py` — crash isolation.** The DPAPI/AES path can hard-crash the process (segfault) on corrupt browser data, so `web_app.py` and `scheduler.py` never call `cookie_browser` in-process. `safe_cookie.safe_find_cookie` shells out to `_cookie_probe.py` via `subprocess` (30s timeout), reads progress logs from stderr and the final cookie JSON from stdout. A subprocess crash can't take down the server/scheduler. Preserve this isolation when touching cookie code. `safe_cookie` is the shared module both `web_app` and `scheduler` import (avoids duplication and circular deps).

**`scheduler.py` — daily auto-booking.** `Scheduler` runs a daemon thread that loops: compute next `execute_at` → wait → re-read config (so web edits take effect) → `_refresh_cookie` (headless-read the dedicated profile, cache to `browser_cookie.json`) → `run_booking(days=0, execute_at=None, browser_cookie=...)`. It passes `execute_at=None` because the scheduler controls timing itself; it passes `browser_cookie` because — unlike the manual web run — nothing else injects a cookie into the scheduled run (this was previously a gap: the scheduler had no cookie source at all). On refresh failure it falls back to the cached `auth.cookie_file`. Appends each run to `booking.log`. State (`enabled`, `status`, `next_run`, `last_result`) is read by the web UI via polling.

**`web_app.py` — stdlib HTTP server + embedded UI.** No framework: `ThreadingHTTPServer` + `BaseHTTPRequestHandler`, with the entire single-page UI as the `INDEX_HTML` string constant. Booking runs are background jobs in the in-memory `JOBS` dict (keyed by uuid, polled via `/api/job?id=`), supporting cancellation through a `threading.Event`. Cookie-related endpoints: `/api/cookie-status` (read + cache to `browser_cookie.json`) and `/api/browser-login` (open the visible login window in the dedicated profile). On startup it auto-starts the `Scheduler` if `automation.auto_daily` is set, and auto-increments the port (8765–8784) if busy, writing the chosen port to `.port` for the launcher scripts. JS-side cache-busting forces `/?v=N&t=...` redirects — bump the `v=N` in both the redirect and `<body data-version>` when changing `INDEX_HTML`.

## Conventions

- All datetimes use `datetime.now().astimezone()` (local tz-aware); booking times are converted to epoch seconds for the API.
- Module-level constants `DEFAULT_*` in `instant_book.py` are the shared defaults; import them rather than re-hardcoding.
- When adding a config field consumed by the UI, update three places: the YAML template in `instant_book.py`, `booking_form_from_config` (read), and `write_booking_values` + its regex key list (write).
