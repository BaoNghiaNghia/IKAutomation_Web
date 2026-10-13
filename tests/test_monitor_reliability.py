"""Monitor reliability regression coverage for large Chrome fleets."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from ik_chrome_auto.dashboard import (
    MONITOR_HARD_DEADLINE_SECONDS,
    MONITOR_MAX_SCAN_ATTEMPTS,
    Dashboard,
)
from ik_chrome_auto.mail_monitor import SCAN_ERROR
from ik_chrome_auto.models import WorkerSnapshot, WorkerState


def _mock_monitor() -> tuple[Dashboard, list[tuple[str, dict]]]:
    dashboard = Dashboard.__new__(Dashboard)
    logged: list[tuple[str, dict]] = []
    dashboard._append_log = lambda _text: None
    dashboard._notify_telegram = lambda *_args, **_kwargs: None
    dashboard._log_monitor_scheduler = lambda event, **fields: logged.append((event, fields))
    dashboard.runner = SimpleNamespace(has_open_session=lambda _profile_id: True)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"p{i}") for i in range(45)],
        profile=lambda pid: SimpleNamespace(name=pid),
    )
    dashboard._monitoring_enabled = True
    dashboard._monitor_in_flight = {"p0": 30}
    dashboard._monitor_started_at = {"p0": 12}
    dashboard._monitor_attempts = {"p0": 1}
    dashboard._monitor_quarantined_profiles = set()
    dashboard._monitor_initialized_profiles = set()
    dashboard._monitor_batch_pending = deque()
    dashboard._monitor_batch_profiles = {"p0"}
    dashboard._monitor_batch_members = ("p0",)
    dashboard._monitor_batch_phase = "combat"
    dashboard._monitor_next_profile_at = 0
    dashboard._monitor_next_batch_at = 0
    dashboard._monitor_cycle_number = 1
    dashboard._monitor_queue = deque()
    return dashboard, logged


def test_scan_error_retries_then_quarantines_only_bad_account() -> None:
    dashboard, log = _mock_monitor()
    snapshot = WorkerSnapshot("p0", WorkerState.READY, "error", monitor_events=(SCAN_ERROR,))
    for attempt in range(1, MONITOR_MAX_SCAN_ATTEMPTS):
        dashboard._monitor_attempts["p0"] = attempt
        dashboard._monitor_in_flight["p0"] = 30
        dashboard._monitor_batch_profiles.add("p0")
        dashboard._handle_monitor_result(snapshot)
        assert list(dashboard._monitor_batch_pending) == ["p0"]
        dashboard._monitor_batch_pending.clear()
        assert not dashboard._monitor_quarantined_profiles
    dashboard._monitor_attempts["p0"] = MONITOR_MAX_SCAN_ATTEMPTS
    dashboard._monitor_in_flight["p0"] = 30
    dashboard._handle_monitor_result(snapshot)

    assert dashboard._monitor_quarantined_profiles == {"p0"}
    assert not dashboard._monitor_in_flight
    assert not dashboard._monitor_batch_profiles
    assert not dashboard._monitor_batch_pending
    assert any(event == "monitor_scheduler_quarantined" for event, _ in log)


def test_hard_timeout_stops_dispatch_but_does_not_unlock_active_renderer(monkeypatch) -> None:
    dashboard, log = _mock_monitor()
    dashboard._monitor_started_at["p0"] = 100.0
    dashboard._monitor_in_flight["p0"] = 110.0
    stopped: list[bool] = []
    cancelled: list[set[str]] = []
    dashboard._stop_monitoring = lambda: stopped.append(True)
    dashboard.runner.cancel_mail_monitor = lambda ids: cancelled.append(ids)
    import ik_chrome_auto.dashboard as module

    monkeypatch.setattr(
        module.time, "monotonic",
        lambda: 100.0 + MONITOR_HARD_DEADLINE_SECONDS + 1.0,
    )
    dashboard._advance_monitoring()

    assert cancelled == [{"p0"}]
    assert stopped == [True]
    assert dashboard._monitor_in_flight == {"p0": 110.0}  # no fake completion
    assert any(event == "monitor_scheduler_hard_timeout" for event, _ in log)


def test_success_resets_consecutive_failure_attempts() -> None:
    dashboard, _ = _mock_monitor()
    dashboard._monitor_attempts["p0"] = MONITOR_MAX_SCAN_ATTEMPTS - 1
    dashboard._handle_monitor_result(
        WorkerSnapshot("p0", WorkerState.READY, "success", monitor_events=("no_new_combat_mail",))
    )
    assert "p0" not in dashboard._monitor_attempts
    assert not dashboard._monitor_quarantined_profiles