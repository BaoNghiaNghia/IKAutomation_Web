# IK Auto 45-profile production capacity review — 2026-10-13

## Measured hardware and failure mode

- DESKTOP-DKDCA09 has 63.88 GiB physical RAM. Windows reports a total
  147.82 GiB commit limit after pagefiles C: 20,423 MB and D: 65,536 MB
  became active. These are *not* guarantees that 45 loaded WebGL games fit.
- A previous heavy session produced 223 Chrome processes with ~80.3 GiB
  aggregate private commit and 0.85 GiB IK Auto private commit. At the
  time, commit reached **92.23/93.56 GiB (98.6%)**, despite 55 GiB
  physical RAM being available. Windows logged five Resource-Exhaustion
  2004 events and chrome.exe KERNELBASE.dll / 0xe0000008 crashes.
- The D: pagefile enlarges available commit but does not create additional
  RAM or GPU VRAM. Windows page faults/swap activity can make a 45-profile
  run *much slower*. GPU size, driver version, and sustained WebGL load
  require a monitored soak test rather than assumption.
- A later snapshot after Chrome exited showed 6.87/147.82 GiB committed,
  and no Chrome processes. This is not evidence of 45-profile stability.

## Startup and steady-state rules

1. Reattach *existing* verified CDP Chrome sessions before launching new
   processes. Never open a second Chrome for a profile with ambiguous
   ownership or identity; do not kill a running browser to repair CDP.
2. Batch **at most two** new profiles at a time on this 64 GiB class host;
   use the resource-aware startup policy's approximately 7-second spacing
   and 30-second batch settle pause, with one-at-a-time slowdown for CPU/GPU
   or physical RAM pressure. These are starting values to tune with logs.
3. **Hard stop** new Chrome launches if Windows committed memory >=90% or
   available commit <4 GiB. Resume automatically only after the next safe
   resource check. A second guard immediately before Popen avoids a race.
4. For each profile, verify a unique CDP port, retained Chrome identity,
   page/frame readiness and game canvas before marking READY. Do not
   automatically launch login/Farm/Monitor for profiles that failed attach.
5. Farm and Monitor should serialize expensive game renderer resizing /
   screenshot capture via leases; return the window to its original size
   after each transaction. CDP ACK is not proof of actual in-game dispatch.
6. Avoid synchronous RAM-trim of active renderer/farm/monitor tasks; use
   bounded Qt queue drains, 60s aggregate health heartbeat and rotating logs.
7. When Chrome crashes, classify per profile, detach CDP without closing
   all Chrome, bound retry/backoff and quarantine the single failing profile.
8. Don't rebuild/activate EXE while IK Auto is active. Use StageOnly and
   hash-verified updater after app exit; never overwrite config or profiles.

## Test gate before claiming full 45 support

- 5, 10, 20, 30, and 45 sequentially opened profiles; record 5-minute
  post-batch commit, physical RAM, GPU VRAM, CDP availability and crashes.
- Maintain 45 for at least 60 minutes; verify p95 attach time, all unique
  active CDP identities, renderer lease queue, per-profile Farm success and
  Monitor progress. Test at least five app restarts while retaining Chrome.
- Confirm there are no Windows Resource-Exhaustion 2004, application
  crashes 1000, WebGL context losses or sustained paging slowdown.
- If headroom drops below the critical threshold, stop launching more
  profiles. 45 is a target, not a mandatory count that overrides safety.

**Current verdict:** the machine has improved commit capacity and bounded
startup policies; 45 Chrome windows are *plausible* but **not yet certified**
under sustained live game/WebGL and Farm+Monitor load.