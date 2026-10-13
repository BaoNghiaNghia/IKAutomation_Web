"""Proof that critical Sync input is acknowledged by each follower's CDP call."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from ik_chrome_auto.models import SyncState, WorkerCommand
from ik_chrome_auto.runner import MultiProfileRunner
from ik_chrome_auto.sync_ack import SyncAckTracker


def test_complete_ack_for_forty_four_followers_is_one_summary() -> None:
    rows: list[tuple[str, dict]] = []
    tracker = SyncAckTracker(lambda event, data: rows.append((event, data)))
    targets = {f"p{i}" for i in range(44)}
    tracker.start("sync1:1", "pointerdown", targets)
    for n, profile in enumerate(sorted(targets)):
        tracker.ack(profile, "sync1:1", True, float(n + 1))
    assert len(rows) == 1
    event, row = rows[0]
    assert event == "sync_input_ack_complete"
    assert row["target_count"] == 44
    assert row["ack_success_count"] == 44
    assert row["ack_missing_profile_ids"] == []
    assert row["cdp_ack_only"] is True
    assert tracker.snapshot()["ack_pending_events"] == 0


def test_failed_or_missing_follower_is_visible_without_false_complete() -> None:
    rows: list[tuple[str, dict]] = []
    tracker = SyncAckTracker(lambda event, data: rows.append((event, data)))
    tracker.start("s:1", "keyup", {"p1", "p2"})
    tracker.ack("p1", "s:1", True, 10.0)
    tracker.ack("p2", "s:1", False, 12.0)
    assert rows[0][0] == "sync_input_ack_incomplete"
    assert rows[0][1]["ack_failed_profile_ids"] == ["p2"]

    tracker.start("s:2", "pointerup", {"p1", "p2"})
    tracker.ack("p1", "s:2", True, 10.0)
    tracker.flush()
    assert rows[-1][1]["ack_missing_profile_ids"] == ["p2"]
    assert tracker.snapshot()["ack_incomplete_events"] == 2


def test_real_runner_fanout_can_reconcile_every_follower_ack() -> None:
    records: list[tuple[str, dict]] = []

    class Follower:
        def __init__(self):
            self.session = object()
            self.commands: list[WorkerCommand] = []

        def submit(self, command: WorkerCommand) -> None:
            self.commands.append(command)

    runner = MultiProfileRunner.__new__(MultiProfileRunner)
    runner.sync_enabled = True
    runner.sync_state = SyncState.ACTIVE
    runner.sync_master_id = "master"
    runner.sync_target_ids = {f"f-{i}" for i in range(44)}
    runner._sync_lock = threading.Lock()
    runner._sync_session_id = "session-1"
    runner._sync_delivery_counter = 0
    runner.event_log = SimpleNamespace(write=lambda evt, row: records.append((evt, row)))
    runner._sync_ack = SyncAckTracker(runner.event_log.write)
    runner.workers = {"master": Follower(), **{pid: Follower() for pid in runner.sync_target_ids}}
    runner.is_attached = lambda _pid: True

    runner._on_input("master", {"type": "pointerdown", "sequence": 9})
    for follower_id in runner.sync_target_ids:
        command = runner.workers[follower_id].commands[0]
        delivery_id = command.payload["event"]["sync_delivery_id"]
        runner._on_sync_delivery_ack(follower_id, delivery_id, True, 8.5)

    reports = [row for event, row in records if event == "sync_input_ack_complete"]
    assert len(reports) == 1
    assert reports[0]["target_count"] == 44
    assert reports[0]["ack_success_count"] == 44
    assert runner.sync_metrics()["ack_completed_events"] == 1
