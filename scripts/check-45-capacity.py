"""Read-only 45-profile capacity gate. Never prints credentials or opens Chrome.

Run from the repository, e.g.:
    .venv\\Scripts\\python.exe scripts\\check-45-capacity.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ik_chrome_auto.config import load_config
from ik_chrome_auto.credential_store import WindowsCredentialStore
from ik_chrome_auto.memory_guard import CommitSnapshot, get_windows_commit_snapshot

TARGET = 45


def evaluate_capacity(
    *,
    configured: int,
    enabled: int,
    credential_rows: Iterable[tuple[str, bool]],
    commit: CommitSnapshot | None,
) -> dict[str, object]:
    """Compute safe readiness using counts only; never store raw usernames."""
    rows = [(username.strip().casefold(), bool(complete)) for username, complete in credential_rows]
    complete_names = [username for username, complete in rows if username and complete]
    unique = len(set(complete_names))
    missing = max(0, TARGET - unique)
    duplicate_count = len(complete_names) - unique
    blocked: list[str] = []
    if enabled < TARGET:
        blocked.append("need_more_configured_profiles")
    if missing:
        blocked.append("need_unique_account_credentials")
    if duplicate_count:
        blocked.append("duplicate_account_credentials")
    if commit is None:
        blocked.append("commit_telemetry_unavailable")
    elif commit.critical:
        blocked.append("commit_memory_critical")

    return {
        "target": TARGET,
        "configured_profiles": configured,
        "enabled_profiles": enabled,
        "credential_entries": len(rows),
        "unique_complete_accounts": unique,
        "missing_unique_accounts": missing,
        "duplicate_complete_accounts": duplicate_count,
        "commit_usage_percent": round(commit.usage_ratio * 100, 1) if commit is not None else None,
        "commit_headroom_gib": round(commit.available_bytes / (1024**3), 2) if commit is not None else None,
        "ready_for_staged_45_profile_test": not blocked,
        "certified_for_60_minute_45_profile_run": False,
        "blockers": blocked,
        "next_steps": [
            "Add missing independent game accounts through IK Auto > Quản lý tài khoản",
            "Configure additional D: pagefile in an administrator maintenance window and reboot safely",
            "After commit is under 90%, open Chrome in small batches and run a monitored 60-minute soak test",
        ] if blocked else [
            "Run a staged 45-profile startup and monitored 60-minute soak test before certifying stability"
        ],
    }


def inspect_install(root: Path) -> dict[str, object]:
    config_path = root / "release" / "IK Auto" / "config.json"
    if not config_path.is_file():
        config_path = root / "config.json"
    config = load_config(config_path)
    try:
        store = WindowsCredentialStore()
    except Exception:
        store = None

    rows: list[tuple[str, bool]] = []
    for profile in config.profiles:
        if not profile.enabled:
            continue
        credential = None
        if store is not None:
            try:
                credential = store.load(profile.id)
            except Exception:
                pass
        rows.append((
            credential.username if credential else "",
            bool(credential and credential.username and credential.password),
        ))

    try:
        commit = get_windows_commit_snapshot()
    except OSError:
        commit = None
    return evaluate_capacity(
        configured=len(config.profiles),
        enabled=sum(p.enabled for p in config.profiles),
        credential_rows=rows,
        commit=commit,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(inspect_install(args.root), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
