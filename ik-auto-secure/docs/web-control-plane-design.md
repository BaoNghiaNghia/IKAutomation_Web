# Web control-plane design, phase 0

The original requirement specification is retained beside this document as
[`farm-automation-web-spec.md`](farm-automation-web-spec.md). This phase adds
the domain contracts that the website will enforce before any API, broker or
Windows Agent implementation is attached.

## Boundary

The web control plane validates and persists intent. It does not know ADB
coordinates, emulator handles, templates, screenshots, or gameplay actions.
Only a Windows Agent can acquire a device lease and decide an input from a
fresh verified frame.

## Implemented domain contracts

`ik_chrome_auto.web_control` provides:

- Versioned `FarmProfile` validation: resource, level and team policy guards.
- `CommandEnvelope`: actor role, idempotency key, agent/device scope,
  optimistic version and deadline validation.
- `DeviceRunState` and explicit transition map; `preflight` cannot jump to
  `running`.
- `DeviceLease`: ownership and expiry guard to be checked by the Agent before
  an input.
- `DeviceSnapshot` and `SnapshotProjector`: event-id deduplication and a
  monotonic sequence rule so stale stream events cannot overwrite the UI.

## Next implementation slice

1. Persist these contracts in PostgreSQL tables and append-only audit/events.
2. Add authenticated HTTP endpoints that only create `CommandEnvelope`s.
3. Add an outbound, TLS-authenticated Windows Agent that consumes commands and
   publishes snapshots; it must not expose ADB to the browser or Internet.
4. Port the bounded workflow into the agent with the fresh-frame/rematch/
   post-action verification guard.
