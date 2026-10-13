"""Regression tests for privacy-safe 45-account capacity checks."""
import importlib.util
from pathlib import Path

from ik_chrome_auto.memory_guard import CommitSnapshot


SPEC = importlib.util.spec_from_file_location(
    "capacity45", Path(__file__).resolve().parents[1] / "scripts" / "check-45-capacity.py"
)
assert SPEC is not None and SPEC.loader is not None
capacity45 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity45)


def test_capacity_rejects_43_profiles_even_when_commit_is_available():
    rows = [(f"user{i}", True) for i in range(43)]
    report = capacity45.evaluate_capacity(
        configured=43, enabled=43, credential_rows=rows,
        commit=CommitSnapshot(limit_bytes=200 * 1024**3, available_bytes=90 * 1024**3),
    )
    assert report["missing_unique_accounts"] == 2
    assert report["ready_for_staged_45_profile_test"] is False
    assert "need_more_configured_profiles" in report["blockers"]
    assert "need_unique_account_credentials" in report["blockers"]
    assert "user1" not in repr(report)


def test_capacity_rejects_duplicate_credentials_and_high_commit():
    rows = [(f"USER{i}", True) for i in range(44)] + [("user0", True)]
    report = capacity45.evaluate_capacity(
        configured=45, enabled=45, credential_rows=rows,
        commit=CommitSnapshot(limit_bytes=200 * 1024**3, available_bytes=10 * 1024**3),
    )
    assert report["unique_complete_accounts"] == 44
    assert report["duplicate_complete_accounts"] == 1
    assert "commit_memory_critical" in report["blockers"]
    assert "duplicate_account_credentials" in report["blockers"]


def test_capacity_allows_staged_test_not_stability_certification():
    report = capacity45.evaluate_capacity(
        configured=45, enabled=45,
        credential_rows=[(f"user{i}", True) for i in range(45)],
        commit=CommitSnapshot(limit_bytes=250 * 1024**3, available_bytes=90 * 1024**3),
    )
    assert report["ready_for_staged_45_profile_test"] is True
    assert report["certified_for_60_minute_45_profile_run"] is False
    assert report["blockers"] == []


def test_capacity_reports_real_credential_counts_not_enabled_profile_count():
    rows = [(f"user{i}", True) for i in range(42)] + [
        ("user_incomplete", False), ("", False), ("", False),
    ]
    report = capacity45.evaluate_capacity(
        configured=45, enabled=45, credential_rows=rows,
        commit=CommitSnapshot(limit_bytes=250 * 1024**3, available_bytes=90 * 1024**3),
    )
    assert report["credential_slots_checked"] == 45
    assert report["credential_entries"] == 43
    assert report["incomplete_credential_entries"] == 1
    assert report["profiles_without_credential_username"] == 2
    assert report["unique_complete_accounts"] == 42
    assert report["missing_unique_accounts"] == 3
    assert report["ready_for_staged_45_profile_test"] is False
    assert "user_incomplete" not in repr(report)


def test_unavailable_commit_fails_closed():
    report = capacity45.evaluate_capacity(
        configured=45, enabled=45,
        credential_rows=[(f"user{i}", True) for i in range(45)], commit=None,
    )
    assert "commit_telemetry_unavailable" in report["blockers"]