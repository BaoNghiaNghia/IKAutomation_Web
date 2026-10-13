# IK Auto: feature audit, diagnostics and startup-only updates

## Feature inventory (2026-10-12)

| Area | Current implementation | Key module |
| --- | --- | --- |
| Account management | Managed Chrome profiles, credential vault and TOTP | `dashboard.py`, `credential_store.py`, `two_factor.py` |
| Profile lifecycle | Open/reattach, close/detach, auto-arrange and multi-profile resource sampling | `runner.py`, `browser.py`, `windows.py` |
| Farm | Browser CDP capture, high-resolution renderer lease, image matching, retry, team selection | `runner.py`, `farm_vision.py`, `farm_matcher.py` |
| Monitor | Email/combat/territory notification scanning and Telegram alerts | `mail_monitor.py`, `dashboard.py`, `telegram.py` |
| Sync | Master/target keyboard, mouse and scroll sync, input ownership gate | `native_input.py`, `runner.py`, `input_engine.py` |
| Desktop | PySide6 Fluent UI, responsive layout, launcher and bundled build | `dashboard.py`, `launcher.py`, `scripts/build-release.ps1` |
| Updates | Git origin/main check on startup, detached updater on restart | `startup_update.py`, `scripts/update-on-launch.ps1` |

This is a source-level inventory, not proof of successful operation against a live game environment.
Hardware-specific Browser/CDP and packaged updater runs must also be exercised on the actual Windows machine.

## Logs

Logs are stored locally and have no external upload:

- `data/logs/lifecycle.jsonl`: application launches/exits, exception hooks (including background threads), redacted UI control activations and update decisions.
- `data/logs/events.jsonl`: worker state changes, command submission, sync ownership, scheduler decisions, dashboard status and error titles.
- `data/logs/farm-*.jsonl`: existing profile-level Farm diagnosis.
- `data/logs/updater.jsonl`: detached PowerShell updater transitions, success, failure category and relaunch.
- `data/screenshots/<profile-id>/farm-debug/`: existing bounded evidence screenshots (only when needed).

JSONL logs use UTC timestamps, rotate at 5 MB, retain three backups by default, and share no secrets through remote services.
Fields containing password, token, cookie, API keys, credentials, recovery codes, or TOTP are redacted.
HTTP URLs keep only scheme and hostname; button labels outside a fixed approved set are recorded as hashes.
Do not attach raw Chrome profiles, config credentials, or exported account lists to a support ticket.

### Diagnostic workflow

1. Filter the most recent `lifecycle.jsonl` records to identify the last run or exception.
2. Correlate by timestamp with `events.jsonl`, worker `profile_id` and command.
3. For Farm/image failures, read the matching per-profile farm log and newest bounded debug image.
4. For update failures, inspect `updater.jsonl`. The old application is relaunched as a fallback when possible.

## Startup update contract

- Update discovery runs **once at each application launch**; no periodic in-app automatic update.
- Repository origin must be the trusted `origin/main` upstream, current branch must be `main`, and the working tree must be clean. No force-push, reset or automatic stash.
- Diverged histories, untracked/modified source, network failure, missing Git, or a release outside the checkout => skip and launch the existing tool.
- When clean and behind, the launcher starts a detached PowerShell updater and exits; the external updater waits for the old process to close before pulling fast-forward changes and rebuilding packaged releases.
- The updater uses `-SkipProfileSync` so existing release Chrome profiles are never overwritten. Auto Farm and monitoring are not automatically restarted.
- The environment guard `IK_AUTO_SKIP_STARTUP_UPDATE=1` prevents an endless relaunch loop.
- Manual **Check for Updates** is informational; actual update is on the next restart.
- Current local modifications (including work in progress) must be reviewed, committed and pushed as appropriate before a future auto-update can operate. Do not automatically include unrelated changes.

## Checks

```cmd
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
git diff --check
```

The update script requires a real Windows integration smoke test with a separate clean test checkout and a test release build.
Do not test an actual Git pull or rebuild against the user's dirty live checkout.

## Production support logging (2026-10-13)

With the desktop tool running, every structured JSONL entry now includes a common
`run_id`, process `pid`, UTC timestamp and thread name. This correlates lifecycle,
worker/CDP, Farm, Sync ACK, Monitor and update events without requiring remote
screen recording or storing input text. A `fleet_health_heartbeat` is emitted
once per 60 seconds to `events.jsonl`, containing only technical counters:
enabled/connected/errors, failed reattachments, pending UI actions, Monitor
quarantine, active Farm and Sync status and aggregate CDP ACK metrics.

**Retention and privacy:**

- Main `events.jsonl`: 10 MB per file, 12 rotated backups, expired backup
  files pruned after 20 days. Lifecycle: 5 MB per file, 6 backups and 20-day
  backup retention. Per-profile Farm logs still have a separate small cap.
- No automatic cloud uploads, keylogging, clipboard capture, Chrome profile
  dumps or video recording are enabled. Logs are local to the running
  application's **own** data directory.
- Password/token/cookie/key fields and URL paths are redacted in JSONL.
  Support exports below expose only allowlisted counts, events and profile IDs,
  not arbitrary original messages. Raw historical logs may predate redaction
  improvements and should be treated as private.
- Rotation is **size-bound as well as time-bound**: the system cannot guarantee
  retention of every event for 20 days when volume exceeds the disk budget.
  Logging happens only while IK Auto or its detached updater runs.

**Create a privacy-safe diagnostic summary on the actual machine** (Windows CMD):

```bat
cd /d D:\IKAutomation_Web
set PYTHONPATH=src
.venv\Scripts\python.exe -m ik_chrome_auto --config "release\IK Auto\config.json" diagnostic-summary
```

This creates `release\IK Auto\data\logs\support-summary.json` and prints
categories of events/errors, last timestamp, affected profile IDs and the
number of log files examined. The command is read-only **except for writing its
summary**; it does not attach to or open a Chrome window. If debugging the
development configuration (currently 45 entries), substitute `config.json`.
Do not overwrite the Release configuration (currently 43 entries) with the
development configuration; the account IDs differ.

**Use CodeLocal:** Pair the production Windows workspace
`DESKTOP-DKDCA09 / D:\IKAutomation_Web`, then ask ChatGPT to inspect
`support-summary.json`, `lifecycle.jsonl`, `events.jsonl`, the newest
per-profile Farm logs and `updater.jsonl`. Debug the correct Release or Dev
data tree, not whichever configuration is easiest to reach.

**Verification:** A passing unit test suite is not a substitute for
45 live Chrome windows. The acceptance gate remains successful browser attach,
critical Sync ACK plus in-game effect verification, Farm/Monitor recovery,
five restart cycles and recovery from a deliberately interrupted update.

### Production one-command support report

Run `scripts\support-summary-prod.cmd` from the workspace root. It uses the
current **Release** config and writes the diagnostic summary under
`release\IK Auto\data\logs\support-summary.json`. It never starts Chrome.
The development configuration has 45 entries while Release currently has 43
with different IDs, so these two configs must not be overwritten into each other.

### Page-crash circuit breaker

Historical Release logs included thousands of `worker_pump_error` events for
`account-42` with `TargetClosedError: Page crashed`. A three-error threshold
now detaches Playwright without closing the retained Chrome window, emits a
single `profile_renderer_crashed` event and marks the worker as ERROR.
Repeated transient pump errors are aggregated rather than written at 25 Hz.
`worker_pump_recovered` logs recovery after an intermittent failure.

A regression test additionally protects against calling
`BrowserContext.close()` on detach, including contexts of Chrome launched by
IK Auto. Users must explicitly choose the “Đóng tabs” action to terminate a
browser. These changes protect unattended operation but do not repair a game
tab already crashed; a human must inspect the tab and Retry.
