"""Production observability: correlation, privacy, retention and support report."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from ik_chrome_auto.diagnostics import fleet_health_payload
from ik_chrome_auto.event_log import SESSION_RUN_ID, JsonLineLog
from ik_chrome_auto.support_report import summarize_logs


def test_all_logs_have_stable_run_id_pid_thread_and_redact_secrets(tmp_path: Path) -> None:
    log = JsonLineLog(tmp_path / "events.jsonl")
    log.write("profile_attach_error", {
        "profile_id": "account-1",
        "password": "must-never-appear",
        "message": "token=other-secret https://ik.playfun.vn/private/path?auth=sensitive",
    })
    log.write("profile_attached", {"profile_id": "account-1"})
    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert len({item["run_id"] for item in lines}) == 1
    assert lines[0]["run_id"] == SESSION_RUN_ID
    assert lines[0]["pid"] == os.getpid()
    assert lines[0]["thread"]
    raw = log.path.read_text(encoding="utf-8")
    assert "must-never-appear" not in raw
    assert "other-secret" not in raw
    assert "/private/path" not in raw


def test_rotated_logs_prune_aged_backups_without_touching_active_log(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("current\n", encoding="utf-8")
    old = tmp_path / "events.jsonl.1"
    old.write_text("old\n", encoding="utf-8")
    recent = tmp_path / "events.jsonl.2"
    recent.write_text("new\n", encoding="utf-8")
    aged = time.time() - 40 * 86400
    os.utime(old, (aged, aged))
    JsonLineLog(path, backups=3, retention_days=20)
    assert path.exists()
    assert not old.exists()
    assert recent.exists()


def test_privacy_preserving_support_summary_does_not_copy_log_payload(tmp_path: Path) -> None:
    log = JsonLineLog(tmp_path / "events.jsonl")
    log.write("profile_attach_error", {
        "profile_id": "account-5",
        "username": "private@example.com",
        "message": "some very private in-game mail",
        "password": "sample",
    })
    log.write("sync_input_ack_complete", {"target_count": 44})
    report = summarize_logs(tmp_path)
    output = json.dumps(report, ensure_ascii=False)
    assert report["events_by_type"]["sync_input_ack_complete"] == 1
    assert report["failures_by_type"]["profile_attach_error"] == 1
    assert report["affected_profile_ids"] == ["account-5"]
    assert "private@example.com" not in output
    assert "very private" not in output
    assert "sample" not in output
    assert report["read_only"] is True


def test_fleet_heartbeat_is_bounded_aggregate_not_login_details() -> None:
    pids = [f"p{i}" for i in range(45)]
    runner = SimpleNamespace(
        has_open_session=lambda pid: int(pid[1:]) < 43,
        sync_enabled=True,
        sync_metrics=lambda: {
            "ack_completed_events": 16,
            "ack_incomplete_events": 2,
            "sync_session_id": "secret-should-not-be-in-heartbeat",
        },
    )
    dashboard = SimpleNamespace(
        runner=runner,
        config=SimpleNamespace(profiles=[
            SimpleNamespace(id=pid, enabled=True, name="private user")
            for pid in pids
        ]),
        rows={"p44": SimpleNamespace(last_state=SimpleNamespace(name="ERROR"))},
        updates=SimpleNamespace(qsize=lambda: 10),
        _monitoring_enabled=True,
        _monitor_in_flight={"p44": 30},
    )
    report = fleet_health_payload(dashboard)
    assert report["cdp_session_ready"] == 43
    assert report["configured_enabled"] == 45
    assert report["error_profile_ids"] == ["p44"]
    assert report["monitor_in_flight_count"] == 1
    assert "secret" not in json.dumps(report)
    assert "private user" not in json.dumps(report)
