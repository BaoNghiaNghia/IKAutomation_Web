"""Farm observer must remain read-only with no raw game or account contents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ik_chrome_auto.farm_observer import observe, write_snapshot


def _event(path: Path, event: str, at: int, **fields: object) -> None:
    row = {"timestamp": datetime.fromtimestamp(at, UTC).isoformat(), "event": event,
           "profile_id": "account-3", **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_observer_reports_repeated_blocking_without_private_payload(tmp_path: Path) -> None:
    now = 1_800_000_000
    log = tmp_path / "farm-masked-user.jsonl"
    _event(log, "started", now - 110)
    for seconds in (100, 90, 80):
        _event(log, "resource_area_navigation_blocked", now - seconds,
               message="Password=private-and-secret; account name")
    _event(log, "detection", now - 10)
    snapshot = observe(tmp_path, now=now)
    assert snapshot["profiles_seen"] == 1
    assert snapshot["profiles_active_in_observed_window"] == 1
    assert snapshot["anomalies"] == [{
        "profile_id": "account-3", "kind": "repeated_resource_area_navigation_blocked",
        "count": 3,
    }]
    output = write_snapshot(tmp_path, snapshot)
    data = output.read_text(encoding="utf-8")
    assert "private-and-secret" not in data
    assert "masked-user" not in data
    assert "password" not in data.lower()


def test_observer_detects_stale_farm_without_touching_browser(tmp_path: Path) -> None:
    now = 1_800_000_000
    log = tmp_path / "farm-a.jsonl"
    _event(log, "started", now - 500)
    result = observe(tmp_path, now=now)
    assert result["anomalies"] == [{
        "profile_id": "account-3", "kind": "farm_log_stale", "age_seconds": 500,
    }]
    _event(log, "stopped", now - 400)
    result = observe(tmp_path, now=now)
    assert not result["anomalies"]


def test_observer_handles_missing_log_directory(tmp_path: Path) -> None:
    result = observe(tmp_path / "missing")
    assert result["status"] == "logs_missing"

def test_long_running_farm_with_recent_detection_is_counted_as_observed(tmp_path: Path) -> None:
    now = 1_800_000_000
    log = tmp_path / "farm-hidden.jsonl"
    # Start occurred before the observation window; regular detection proves
    # the worker is currently emitting data without revealing any game text.
    _event(log, "started", now - 2000)
    _event(log, "detection", now - 30)
    snapshot = observe(tmp_path, now=now)
    assert snapshot["profiles_active_in_observed_window"] == 1
    assert snapshot["profiles"][0]["last_event"] == "detection"