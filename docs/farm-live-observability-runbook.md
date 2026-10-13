# IK Auto — AutoFarm production observation & recovery runbook
**Updated:** 2026-10-13
**Machine:** DESKTOP-DKDCA09, `D:\IKAutomation_Web`
**Normative spec:** `farm-browser-implementation-contract.md` and `farm-automation-browser-spec.md`.
The older LDPlayer/ADB `farm-automation-web-spec.md` is historical context only.

## Scope and non-interference

The packaged Windows application owns Farm, Chrome/CDP, and input. The observer
is a separate **read-only** Python process: it reads bounded tails of local
`farm-*.jsonl` files and atomically writes an allowlisted JSON status.
It must **never** attach CDP, focus a browser, send keyboard/mouse input,
restart Chrome, update account data, or apply source-code edits.

- Exact runtime logs: `release\IK Auto\data\logs\` (43 Release profiles today).
- Development logs/config are independent: `data\logs\` / `config.json`
  (45 configured IDs today). **Do not merge or overwrite the two.**
- Run once: `scripts\observe-farm-live.cmd`.
- Install 60-second automatic snapshots for the currently logged-on Windows
  account: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-farm-observer.ps1`.
- Remove only the scheduled observer: add `-Remove` to that command.
- Output: `release\IK Auto\data\logs\farm-observer-snapshot.json`.
- The observer reads at most 100 Farm files and 200 KB from each file tail,
  and returns safe profile IDs, event names, counts, time windows and derived
  anomaly names. **No raw messages, screenshots, coordinates, account names,
  cookies, URLs, passwords or tokens** are copied into the snapshot.
- A 60-second scheduled diagnostic is not a 24/7 ChatGPT monitoring agent.
  ChatGPT can inspect the latest files when CodeLocal is connected and asked.
  The Windows scheduled task itself continues making summaries between chats.

## Production decision table (one verified dispatch per cycle)

| Step | Required live evidence | Success event/postcondition | Failure action |
| --- | --- | --- | --- |
| Preflight | Attached CDP, fresh usable game canvas | `detection`, verified City/World Map | No input on Unknown; log and bounded recheck |
| World Map | Fresh map/canvas anchor (no unrelated HUD cross-match) | `world_map_verified` | At most bounded verified tap; diagnostic on timeout |
| Roster | Distinct per-row Ready/Busy evidence, stable row indices 1–4 | Initial verified Ready team selected once | No Ready → waiting; do not invent team |
| Search category | Fresh resource panel/category evidence | `resource_tab_verified` | Do not select resource from monster category |
| Resource & level | Actual active-resource template + level evidence | `tap_resource` followed by verified active match | No Search until the correct selected glyph verified |
| Checkbox | Verified unchecked or checked glyph on fresh frame | `search_target_checkbox_verified` | No blind repeat; diagnostic after bounded retries |
| Search | Fresh enabled Search and confirmed filter | `tap_find_resource` → popup or classified no-result | For no-result, try next resource in SAME level |
| Area | Only **after four resource misses**, current verified World/City UI and pin fields | `search_round_area_relocated` | Max three nonrepeating points; bounded wait/cooldown |
| Level | Target progression **6 → 7 → 8** requires matching level control and postcondition | Must verify game UI shows newly selected level | Missing template/control → block, **never claim changed** |
| Team selection | Fresh team panel + expected team row/selected border | Confirm expected team selected | Never assume click succeeded from CDP ACK alone |
| Dispatch | Fresh Gather/Collect control and bounded postcondition | `dispatch_verified` and new roster scan | Do not issue a second dispatch from an uncertain state |
| Next cycle | Post-dispatch roster refreshed | New randomized resource order after verified march | Yield renderer so another profile can run |

**Effective production limitations (do not overstate):**
- Code currently recognizes image templates for level 6 and 7; level 8 and
  full level-transition controls lack verified evidence. Planned 6→7→8 is
  a **contract**, not a completed end-to-end feature. Automation must wait
  safely rather than force the next numeric level.
- Some World Map/search/checkbox layout fallback controls use guarded canvas
  ratios after a verified screen/panel instead of fresh matched button bounds.
  These are engineering risks requiring further screenshot QA.
- The existing dispatch postcondition may accept a closed Team panel and
  verified World Map when the roster cannot be read. This means `dispatch_verified`
  is not always physical proof of marching; needs independent roster/march
  evidence before a strict production certification.
- Historical browser contract specifies navigation via City before Continent
  Map, but the current web portal exposes a verified Continent Map icon on
  World Map. Either route requires verified current screen and actionable icon;
  no unconditional City toggle or legacy ADB minimap shortcut is permitted.

## Recovery classification

| Observer anomaly | Meaning | Safe response |
| --- | --- | --- |
| `repeated_resource_area_navigation_blocked` | Map icon/overlay/pin/coordinate verification failing | Backoff; inspect screenshot and template before input |
| `repeated_search_round_area_waiting` | Relocation repeatedly unverified | Max 6 checks, then diagnostic and Farm cycle cooldown |
| `repeated_farm_canvas_too_small` | Canvas too small for reliable vision | Request verified renderer resize only; do not interpret empty roster |
| `repeated_retry` | Technical reset loop | Check root cause and per-profile circuit/recovery |
| `farm_log_stale` | Active-inferred Farm log idle >180s | Verify process, renderer lease and stalled capture without clicking |
| `repeated_search_result_unverified` | Search result changed or not recognized | Wait/rematch; never click Search twice blindly |

A business `no-result` is *not* a technical error. Do not restart Chrome
just because a resource or team is unavailable.

## Evidence sampled on 2026-10-13

The live observer saw eight profile logs with Farm activity during its
15-minute sample; all eight had a subsequent `stopped` event.
`account-7` recorded 17 `resource_area_navigation_blocked` and
17 `search_round_area_waiting` events. This is observed historical
behaviour for that window, **not** proof of the current Farm state
or of an already repaired release binary.

## Safe rollout and auto-correction boundary

1. Keep read-only observation running in its own scheduled task.
2. Group repeated failures by event/profile and correlate with log timestamps.
3. Fix deterministic code issues with regression tests and full pytest/lint.
4. **Never replace the running IK Auto executable.** Stage the next version
   separately, validate its hash, preserve old binaries, and apply only after
   IK Auto exits; never terminate/restart Chrome to complete an update.
5. On first use of a new Farm version, validate **one profile** and evidence
   screenshots of each transition; then widen to the full Release fleet.
6. If the vision evidence is missing, leave Farm waiting safely, not
   synthesizing clicks. An unattended helper may adapt approved retry intervals
   or classify errors, but must not generate and execute unreviewed gameplay
   controls or hot-patch running code.
7. Successful unit tests prove code logic, not real game dispatch. Live CDP,
   human-visible game-state verification and long-run soak tests remain required.

## Quick commands (run from workspace root)

```bat
scripts\observe-farm-live.cmd
scripts\support-summary-prod.cmd
.venv\Scripts\python.exe -m pytest -q
```

## Staged build and automatic safe activation

While `IK Auto.exe` is open, **do not run the normal release deployment**.
The safe packaging command is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-release.ps1 -NoDesktopShortcut -SkipProfileSync -StageOnly
```

`-StageOnly` builds and SHA256-records the new executable and full library
tree into `release\.ik-auto-app-next\`. It does not overwrite the current
`release\IK Auto\IK Auto.exe` and does not touch `config.json` or profiles.

Verify the candidate before enabling automatic activation:

```bat
.venv\Scripts\python.exe src\ik_chrome_auto\staged_update.py --root . --verify-only
```

Install the low-impact scheduled safe activator (one check per minute):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-safe-update-task.ps1
```

The activator checks `tasklist` for **any** running `IK Auto.exe`; it
returns without modifying files if a running process is detected or process
inspection fails. Only when IK Auto is no longer running does it verify
the staged EXE SHA256 and every `_internal` file hash, preserve previous
binaries, and swap exactly the EXE and `_internal` directory. A move failure
rolls the old binary paths back. Chrome processes are not inspected for
mutation and never terminated by the activator.

The scheduled task **does not relaunch IK Auto**; the user's next normal
launch uses the new binary after activation. If the user immediately
reopens before the next one-minute check, activation remains pending
until a later app exit. This is intentional: no unrequested hot update,
restart, loss of Farm tasks or simultaneous CDP ownership.

`powershell -File scripts/install-safe-update-task.ps1 -Remove` unregisters
only this task. The immutable source of truth is the tested Git revision;
the staging manifest is a local integrity check rather than a network
signature or proof that gameplay itself has passed live validation.

## Critical Windows commit exhaustion (2026-10-13)

**Field evidence, read-only Windows diagnostics:**

- UI showed Chrome sad tabs, blank windows and exception `0xe0000008`.
- Windows Application Error 1000 recorded `chrome.exe` with
  `KERNELBASE.dll`, `0xe0000008`; several crashes clustered
  around 04:10-04:12 UTC. KERNELBASE is the exception boundary,
  not proven to be the defective component.
- System Event Log recorded five
  `Microsoft-Windows-Resource-Exhaustion-Detector` Event 2004 entries
  around the Chrome failures, plus one WHEA-Logger event requiring separate
  attention if failures persist after the memory issue is resolved.
- `GlobalMemoryStatusEx` reported 63.88 GiB physical RAM,
  55.23 GiB physically free, **92.23 / 93.56 GiB committed (98.6%)**,
  only 1.33 GiB Windows commit available.
- 223 Chrome processes had about **80.3 GiB total Private Bytes**,
  20 of them exceeding 1 GiB individually. IK Auto itself ~0.85 GiB.
  Task Manager's RAM usage was misleading because COMMIT headroom, not
  physical resident RAM, was exhausted.
- System-managed paging file resides on `C:` at ~30,388 MB; `C:`
  has only 11.6 GiB free while `D:` has ~413.4 GiB free.

**Remediation requiring local Administrator/UAC approval:**

1. Leave existing Chrome sessions intact while collecting data.
2. On the remote desktop, open **PowerShell as Administrator** and execute:
   `powershell -NoProfile -ExecutionPolicy Bypass -File D:\IKAutomation_Web\scripts\configure-pagefile-d.ps1 -Apply`
3. The prepared script preserves the C: paging file and requests a D:
   pagefile with 65,536 MB initial / 98,304 MB maximum size. It does
   not reboot. Local CodeLocal runtime is **not elevated**, so the
   change is not applied by the background assistant.
4. After saving all work, manually restart Windows once. Recheck
   `scripts\inspect-pagefile-settings.ps1` and
   `.venv\Scripts\python.exe scripts\inspect-commit-ctypes.py`.
   A larger commit limit must be visible before reopening all profiles.
5. Bring up 1-2 Chrome profiles first; check error logs and GPU. If
   crashes persist while commit is healthy, investigate WHEA and NVIDIA
   driver/hardware issues separately.

**Application prevention:** `memory_guard.py` checks Windows commit and
blocks only **new managed Chrome launches** when less than 4 GiB commit
remains or 90% is used. Existing CDP reattachment is unaffected.
The running EXE is **not hot patched**; this source change needs a new
verified Windows build/deployment. The observer now records
`commit_used_percent`, `commit_available_gib`, and
`commit_critical` each minute. The previous scheduled staged updater
was disabled until this newer guarded build is verified and Windows
paging is repaired.

No mass Chrome termination, registry modification or automatic reboot
was performed during this investigation.