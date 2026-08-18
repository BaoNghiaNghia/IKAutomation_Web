# IK Auto Secure

Windows desktop tool for operating separate Chrome profiles used with the IK
portal. It retains the profile manager, window controls, input synchronisation
and 2048 helper while making data collection explicit and bounded.

The browser-farming architecture and migration plan are documented in
[`docs/farm-automation-browser-spec.md`](docs/farm-automation-browser-spec.md).

## Safety defaults

- Only `http`/`https` URLs whose hostname matches `capture.allowed_hosts` are
  opened or observed. Matching is hostname-based, so a URL that merely contains
  an allowed domain in its path or query is rejected.
- Network/WebSocket/message capture and response-body capture are disabled by
  default. Turn both on only for a short, supported diagnostic session and
  treat the generated logs as sensitive.
- JSONL logs rotate at 5 MB with three retained backups. Snapshots retain only
  the configured newest files (`capture.snapshot_retention`, default `50`).
- Chrome automation-identifying banners are not hidden by default. The tool
  does not attempt to bypass game controls, access restrictions, or terms.
- Browser profiles contain login cookies. They are excluded from Git; keep the
  `data` folder on an encrypted, access-controlled Windows account.

## Setup and run

1. Copy `config.example.json` to `config.json`.
2. Review `target_url` and the smallest necessary `capture.allowed_hosts`.
3. Run `setup.cmd`, then `run.cmd`.

For one safe preparation/check command, run:

```powershell
.\run-browser-check.ps1
```

It prepares the environment, runs Doctor and browser-worker tests, then opens
the dashboard. Add `-NoDashboard` to stop after the checks.

Run tests from an activated virtual environment:

```powershell
python -m pytest -q
```

This project is intended for authorised automation only. Review the target
service's terms and use separate profiles for separate accounts.
