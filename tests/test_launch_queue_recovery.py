"""Bulk launch must survive a stopped or silent Chrome worker."""
from __future__ import annotations

import time
from collections import deque
from types import SimpleNamespace

import ik_chrome_auto.dashboard as dashboard_module
from ik_chrome_auto.dashboard import Dashboard
from ik_chrome_auto.farm_launch_policy import FarmLaunchPolicy
from ik_chrome_auto.models import CommandKind, WorkerState


class Button:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = False
    def setText(self, text: str) -> None:
        self.text = text
    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


def make_opening_dashboard(monkeypatch):
    dashboard = Dashboard.__new__(Dashboard)
    commands = []
    messages = []
    dashboard.runner = SimpleNamespace(
        submit=lambda pid, cmd: commands.append((pid, cmd)),
        has_open_session=lambda pid: pid == "account-19",
    )
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_launch_policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
    dashboard._farm_open_queue = deque(["account-21"])
    dashboard._farm_open_states = {
        "account-19": WorkerState.READY, "account-20": WorkerState.STOPPED,
    }
    dashboard._farm_batch_profiles = {"account-19", "account-20"}
    dashboard._farm_batch_submitted = 2
    dashboard._farm_batch_limit = 2
    dashboard._farm_batch_resume_at = 0.
    dashboard._farm_next_open_at = 0.
    dashboard._farm_resource_pause_started = 0.
    dashboard._farm_resource_pause_reason = None
    dashboard._latest_profile_cpu_percent = 0.
    dashboard._cached_gpu_percent = lambda: 0.
    dashboard._append_log = messages.append
    dashboard._set_labeled_action_icon = lambda *_args: None
    dashboard.farm_launcher = Button()
    monkeypatch.setattr(dashboard_module, "get_windows_commit_snapshot", lambda: None)
    monkeypatch.setattr(dashboard_module, "get_system_memory_status", lambda: SimpleNamespace(
        available_bytes=40 * (1024**3), load_percent=20.0,
    ))
    return dashboard, commands, messages


def test_stopped_profile_does_not_block_next_batch(monkeypatch) -> None:
    dashboard, commands, _ = make_opening_dashboard(monkeypatch)
    dashboard._advance_farm_opening()
    assert commands == [("account-21", CommandKind.OPEN)]
    assert dashboard._farm_open_queue == deque([])
    assert dashboard._farm_open_states["account-20"] == WorkerState.STOPPED


def test_silent_starting_worker_times_out_and_queue_continues(monkeypatch) -> None:
    dashboard, commands, logs = make_opening_dashboard(monkeypatch)
    dashboard._farm_open_states["account-20"] = WorkerState.STARTING
    dashboard._farm_next_open_at = time.monotonic() - 190
    dashboard._farm_batch_resume_at = time.monotonic() - 190
    dashboard._advance_farm_opening()
    assert dashboard._farm_open_states["account-20"] == WorkerState.ERROR
    assert commands == [("account-21", CommandKind.OPEN)]
    assert any("180 giây" in line for line in logs)


def test_finish_opening_with_one_failure_keeps_successful_sessions() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    lines = []
    arranged = []
    logged = []
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_open_queue = deque()
    dashboard._farm_launch_profiles = {"account-19", "account-20"}
    dashboard._farm_open_states = {
        "account-19": WorkerState.READY,
        "account-20": WorkerState.STOPPED,
    }
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda pid: pid == "account-19",
        arrange_windows=lambda columns, **kwargs: arranged.append((columns, kwargs)) or 1,
    )
    dashboard._farm_arrange_columns = 5
    dashboard.config = SimpleNamespace(profiles=[
        SimpleNamespace(id="account-19"), SimpleNamespace(id="account-20")
    ])
    dashboard._append_log = lines.append
    dashboard._log_monitor_scheduler = lambda name, **details: logged.append((name, details))
    dashboard._set_labeled_action_icon = lambda *_args: None
    dashboard._advance_farm_login = lambda: None
    dashboard.farm_launcher = Button()
    dashboard.farm_all_button = Button()
    dashboard._finish_farm_opening_if_ready()
    assert dashboard._farm_launcher_phase == "logging_in"
    assert dashboard._farm_login_queue == deque(["account-19"])
    assert arranged[0][1]["profile_ids"] == {"account-19"}
    assert any("1/2" in message for message in lines)
    assert logged == [("profile_open_partial", {"ready_count": 1, "failed_count": 1})]
