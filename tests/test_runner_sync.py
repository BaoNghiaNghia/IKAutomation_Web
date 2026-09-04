from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

import ik_chrome_auto.runner as runner_module
from ik_chrome_auto.models import CommandKind, WorkerCommand
from ik_chrome_auto.runner import MultiProfileRunner, ProfileWorker
from ik_chrome_auto.windows import ProcessResourceUsage
from ik_chrome_auto.windows import WindowRect


@dataclass
class FakeWorker:
    session: object | None
    commands: list[WorkerCommand] = field(default_factory=list)

    def submit(self, command: WorkerCommand) -> None:
        self.commands.append(command)


def make_runner() -> MultiProfileRunner:
    runner = MultiProfileRunner.__new__(MultiProfileRunner)
    runner.sync_enabled = True
    runner.sync_master_id = "master"
    runner.sync_target_ids = {"follower-open", "follower-closed"}
    runner._sync_lock = threading.Lock()
    runner.event_log = SimpleNamespace(write=lambda _event, _payload: None)
    runner.workers = {
        "master": FakeWorker(object()),
        "follower-open": FakeWorker(object()),
        "follower-closed": FakeWorker(None),
    }
    return runner


def test_sync_routes_master_event_only_to_open_followers() -> None:
    runner = make_runner()
    event = {"type": "pointerdown", "canvas": {"ratio_x": 0.5, "ratio_y": 0.5}}

    runner._on_input("master", event)

    follower = runner.workers["follower-open"]
    assert len(follower.commands) == 1
    assert follower.commands[0].kind == CommandKind.SYNC_INPUT
    assert follower.commands[0].payload["event"] == event
    assert runner.workers["master"].commands == []
    assert runner.workers["follower-closed"].commands == []


def test_sync_dispatch_log_contains_source_ratio_for_remote_diagnostics() -> None:
    runner = make_runner()
    records: list[tuple[str, dict[str, object]]] = []
    runner.event_log = SimpleNamespace(
        write=lambda event, payload: records.append((event, payload))
    )
    event = {
        "sequence": 17,
        "type": "pointerdown",
        "canvas": {
            "ratio_x": 0.75,
            "ratio_y": 0.5,
            "css_width": 1920.0,
            "css_height": 1080.0,
        },
    }

    runner._on_input("master", event)

    assert records == [
        (
            "sync_input_dispatched",
            {
                "master_profile_id": "master",
                "type": "pointerdown",
                "target_count": 1,
                "sequence": 17,
                "source": {
                    "ratio_x": 0.75,
                    "ratio_y": 0.5,
                    "css_width": 1920.0,
                    "css_height": 1080.0,
                },
            },
        )
    ]


def test_sync_routes_keyboard_event_to_open_followers() -> None:
    runner = make_runner()
    event = {
        "type": "keydown",
        "keyboard": {"key": "Enter", "code": "Enter", "key_code": 13},
    }

    runner._on_input("master", event)

    command = runner.workers["follower-open"].commands[0]
    assert command.kind == CommandKind.SYNC_INPUT
    assert command.payload["event"] == event


def test_sync_routes_input_only_to_selected_open_followers() -> None:
    runner = make_runner()
    runner.workers["follower-other"] = FakeWorker(object())
    runner.sync_target_ids = {"follower-open"}

    runner._on_input("master", {"type": "pointerdown"})

    assert len(runner.workers["follower-open"].commands) == 1
    assert runner.workers["follower-other"].commands == []


def test_enable_sync_keeps_only_selected_targets_and_marks_master_as_source() -> None:
    runner = make_runner()
    runner.sync_enabled = False
    runner.sync_master_id = None
    runner.sync_target_ids.clear()

    runner.enable_sync("master", {"follower-open", "missing", "master"})

    assert runner.sync_enabled is True
    assert runner.sync_master_id == "master"
    assert runner.sync_target_ids == {"follower-open"}
    assert runner.workers["master"].commands[-1].payload == {"enabled": True}
    assert runner.workers["follower-open"].commands == []


def test_disable_sync_only_reconfigures_the_previous_master() -> None:
    runner = make_runner()

    runner.disable_sync()

    assert runner.sync_enabled is False
    assert runner.sync_master_id is None
    assert runner.sync_target_ids == set()
    assert runner.workers["master"].commands[-1].payload == {"enabled": False}
    assert runner.workers["follower-open"].commands == []
    assert runner.workers["follower-closed"].commands == []


def test_profile_worker_coalesces_stale_pointer_moves_before_reliable_input() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.commands = queue.Queue()
    worker._sync_move_lock = threading.Lock()
    worker._sync_pending_move = None
    worker._sync_move_command_queued = False
    worker.submit = worker.commands.put

    worker.submit_synced_input({"type": "pointermove", "sequence": 1})
    worker.submit_synced_input({"type": "pointermove", "sequence": 2})
    worker.submit_synced_input({"type": "pointerup", "sequence": 3})

    assert worker.commands.qsize() == 2
    marker = worker.commands.get_nowait()
    assert marker.payload == {"coalesced_pointer_move": True}
    assert worker._take_pending_sync_move() == {"type": "pointermove", "sequence": 2}
    pointer_up = worker.commands.get_nowait()
    assert pointer_up.payload["event"] == {"type": "pointerup", "sequence": 3}


def test_large_sync_fans_out_every_move_and_never_drops_pointer_up() -> None:
    runner = make_runner()
    followers = {f"follower-{index}" for index in range(44)}
    runner.workers = {
        "master": FakeWorker(object()),
        **{profile_id: FakeWorker(object()) for profile_id in followers},
    }
    runner.sync_target_ids = followers
    runner._on_input("master", {"type": "pointermove", "sequence": 1})
    runner._on_input("master", {"type": "pointermove", "sequence": 2})
    runner._on_input("master", {"type": "pointerup", "sequence": 3})

    assert all(len(runner.workers[profile_id].commands) == 3 for profile_id in followers)
    assert all(
        runner.workers[profile_id].commands[-1].payload["event"]["type"] == "pointerup"
        for profile_id in followers
    )


def test_sync_ignores_non_master_event() -> None:
    runner = make_runner()

    runner._on_input("follower-open", {"type": "pointerdown"})

    assert all(not worker.commands for worker in runner.workers.values())


def test_runner_blocks_autonomous_input_while_sync_owns_profiles() -> None:
    runner = make_runner()
    events = []
    runner.event_log = SimpleNamespace(
        write=lambda event, payload: events.append((event, payload))
    )

    with pytest.raises(RuntimeError, match="quyền điều khiển"):
        runner.submit("follower-open", CommandKind.START_FARM)

    assert runner.workers["follower-open"].commands == []
    assert events == [
        (
            "input_owner_conflict_blocked",
            {
                "profile_id": "follower-open",
                "requested": "start_farm",
                "current_owner": "sync",
            },
        )
    ]


def test_runner_blocks_monitor_engine_while_sync_owns_profiles() -> None:
    runner = make_runner()

    with pytest.raises(RuntimeError, match="tắt đồng bộ"):
        runner.enable_mail_monitor({"follower-open"})


def test_follower_input_repairs_stale_runtime_and_retries_once() -> None:
    class FlakySession:
        def __init__(self) -> None:
            self.attempts = 0
            self.repairs = 0

        def apply_synced_input(self, _event: dict[str, object]) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("frame detached")

        def repair_synced_input_runtime(self) -> None:
            self.repairs += 1

    events: list[tuple[str, dict[str, object]]] = []
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = FlakySession()
    worker.profile = SimpleNamespace(id="follower")
    worker.event_log = SimpleNamespace(
        write=lambda event, payload: events.append((event, payload))
    )

    worker._apply_synced_input_with_retry({"type": "pointerdown"})

    assert worker.session.attempts == 2
    assert worker.session.repairs == 1
    assert events == [
        (
            "sync_input_recovered",
            {"profile_id": "follower", "type": "pointerdown"},
        )
    ]


def test_global_drag_toggle_routes_to_every_profile() -> None:
    runner = make_runner()
    runner.config = type(
        "Config",
        (),
        {
            "profiles": [
                type("Profile", (), {"id": "master"})(),
                type("Profile", (), {"id": "follower-open"})(),
                type("Profile", (), {"id": "follower-closed"})(),
            ]
        },
    )()

    opened = runner.set_all_drag_items_visible(False)

    assert opened == 2
    assert runner.drag_items_visible is False
    assert all(
        worker.commands[-1].kind == CommandKind.SET_DRAG_ITEM
        and worker.commands[-1].payload == {"visible": False}
        for worker in runner.workers.values()
    )


def test_global_scrollbar_toggle_routes_to_every_profile() -> None:
    runner = make_runner()
    runner.config = type(
        "Config",
        (),
        {
            "profiles": [
                type("Profile", (), {"id": "master"})(),
                type("Profile", (), {"id": "follower-open"})(),
                type("Profile", (), {"id": "follower-closed"})(),
            ]
        },
    )()

    opened = runner.set_all_scrollbars_visible(False)

    assert opened == 2
    assert runner.scrollbars_visible is False
    assert all(
        worker.commands[-1].kind == CommandKind.SET_SCROLLBARS
        and worker.commands[-1].payload == {"visible": False}
        for worker in runner.workers.values()
    )


def test_resource_overview_sums_open_profile_process_trees(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="master"), SimpleNamespace(id="closed")]
    )
    runner.workers = {
        "master": FakeWorker(SimpleNamespace(window_handle=101)),
        "closed": FakeWorker(None),
    }
    runner._resource_cpu_samples = {}
    monkeypatch.setattr(runner_module, "snapshot_process_parents", lambda: {2: 1})
    monkeypatch.setattr(
        runner_module,
        "get_window_process_tree_usage",
        lambda _hwnd, _parents: ProcessResourceUsage((1, 2), 300 * 1_048_576, 4.0),
    )

    overview = runner.resource_overview()

    assert overview.total_profiles == 2
    assert overview.opened_profiles == 1
    assert overview.process_count == 2
    assert overview.ram_bytes == 300 * 1_048_576
    assert overview.profiles[0].opened is True
    assert overview.profiles[1].opened is False


def test_trim_ram_routes_each_open_window_once(monkeypatch) -> None:
    runner = make_runner()
    runner.workers = {
        "one": FakeWorker(SimpleNamespace(window_handle=101)),
        "two": FakeWorker(SimpleNamespace(window_handle=202)),
        "closed": FakeWorker(None),
    }
    calls: list[int] = []
    monkeypatch.setattr(runner_module, "snapshot_process_parents", lambda: {})
    monkeypatch.setattr(
        runner_module,
        "trim_window_process_tree",
        lambda hwnd, _parents: calls.append(hwnd) or 3,
    )

    assert runner.trim_all_profile_memory() == 6
    assert calls == [101, 202]


def test_window_toggle_restores_all_when_every_window_is_minimized(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="one"), SimpleNamespace(id="two")]
    )
    runner.workers = {
        "one": FakeWorker(SimpleNamespace(window_handle=101)),
        "two": FakeWorker(SimpleNamespace(window_handle=202)),
    }
    monkeypatch.setattr(runner_module, "is_window_minimized", lambda _hwnd: True)
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        runner_module,
        "set_window_minimized",
        lambda hwnd, minimized: calls.append((hwnd, minimized)) or True,
    )

    assert runner.toggle_all_profile_windows() == (False, 2)
    assert calls == [(101, False), (202, False)]


def test_window_toggle_minimizes_all_when_any_window_is_visible(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="one"), SimpleNamespace(id="two")]
    )
    runner.workers = {
        "one": FakeWorker(SimpleNamespace(window_handle=101)),
        "two": FakeWorker(SimpleNamespace(window_handle=202)),
    }
    monkeypatch.setattr(
        runner_module, "is_window_minimized", lambda hwnd: hwnd == 101
    )
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        runner_module,
        "set_window_minimized",
        lambda hwnd, minimized: calls.append((hwnd, minimized)) or True,
    )

    assert runner.toggle_all_profile_windows() == (True, 2)
    assert calls == [(101, True), (202, True)]


def test_restore_profile_windows_only_restores_minimized_farm_targets(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[
            SimpleNamespace(id="farm-minimized"),
            SimpleNamespace(id="farm-visible"),
            SimpleNamespace(id="other-minimized"),
        ]
    )
    runner.workers = {
        "farm-minimized": FakeWorker(SimpleNamespace(window_handle=101)),
        "farm-visible": FakeWorker(SimpleNamespace(window_handle=202)),
        "other-minimized": FakeWorker(SimpleNamespace(window_handle=303)),
    }
    monkeypatch.setattr(
        runner_module,
        "is_window_minimized",
        lambda hwnd: hwnd in {101, 303},
    )
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        runner_module,
        "set_window_minimized",
        lambda hwnd, minimized: calls.append((hwnd, minimized)) or True,
    )

    restored = runner.restore_profile_windows({"farm-minimized", "farm-visible"})

    assert restored == 1
    assert calls == [(101, False)]


def test_arrange_windows_balances_profiles_across_two_monitors(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"p{index}") for index in range(4)]
    )
    runner.windows_topmost = False
    runner.workers = {
        f"p{index}": FakeWorker(SimpleNamespace(window_handle=100 + index))
        for index in range(4)
    }
    rect = WindowRect(0, 0, 500, 281)
    monkeypatch.setattr(runner_module, "get_window_rect", lambda _hwnd: rect)
    monkeypatch.setattr(runner_module, "get_visible_window_rect", lambda _hwnd: rect)
    monkeypatch.setattr(
        runner_module,
        "get_monitor_work_areas",
        lambda: (WindowRect(0, 0, 1000, 600), WindowRect(1000, 0, 2000, 600)),
    )
    moves: list[tuple[int, int, int, bool]] = []
    monkeypatch.setattr(
        runner_module,
        "move_window_outer",
        lambda hwnd, x, y, _width, _height, **kwargs: moves.append(
            (hwnd, x, y, kwargs["resize"])
        ),
    )

    assert runner.arrange_windows(2) == 4
    assert moves == [
        (101, 500, 0, False),
        (102, 1000, 0, False),
        (103, 1500, 0, False),
    ]


def test_large_arrangement_throttles_webgl_resizes_in_five_window_batches(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"p{index}") for index in range(11)]
    )
    runner.windows_topmost = False
    runner.workers = {
        f"p{index}": FakeWorker(SimpleNamespace(window_handle=200 + index))
        for index in range(11)
    }
    rect = WindowRect(0, 0, 500, 281)
    monkeypatch.setattr(runner_module, "get_window_rect", lambda _hwnd: rect)
    monkeypatch.setattr(runner_module, "get_visible_window_rect", lambda _hwnd: rect)
    monkeypatch.setattr(
        runner_module,
        "get_monitor_work_areas",
        lambda: (WindowRect(0, 0, 1200, 800),),
    )
    resize_flags: list[bool] = []
    monkeypatch.setattr(
        runner_module,
        "move_window_outer",
        lambda _hwnd, _x, _y, _width, _height, **kwargs: resize_flags.append(
            kwargs["resize"]
        ),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", sleeps.append)

    assert runner.arrange_windows(6) == 11
    assert resize_flags == [True] * 11
    assert sleeps.count(0.12) == 11
    assert sleeps.count(1.25) == 2


def test_progressive_arrangement_reserves_final_grid_size(monkeypatch) -> None:
    runner = make_runner()
    runner.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"p{index}") for index in range(6)]
    )
    runner.windows_topmost = False
    runner.workers = {
        "p0": FakeWorker(SimpleNamespace(window_handle=100)),
        "p1": FakeWorker(SimpleNamespace(window_handle=101)),
    }
    rect = WindowRect(0, 0, 500, 281)
    monkeypatch.setattr(runner_module, "get_window_rect", lambda _hwnd: rect)
    monkeypatch.setattr(runner_module, "get_visible_window_rect", lambda _hwnd: rect)
    monkeypatch.setattr(
        runner_module,
        "get_monitor_work_areas",
        lambda: (WindowRect(0, 0, 1000, 600),),
    )
    moves: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(
        runner_module,
        "move_window_outer",
        lambda hwnd, x, y, width, height, **_kwargs: moves.append(
            (hwnd, x, width, height)
        ),
    )

    assert runner.arrange_windows(
        2,
        profile_ids={"p0", "p1"},
        layout_profile_ids={f"p{index}" for index in range(6)},
    ) == 2

    # Six final slots need three rows, so the first two profiles start at the
    # final 355x199 renderer size instead of first growing to 500x281.
    assert moves == [(100, 0, 355, 199), (101, 355, 355, 199)]


class FakeLog:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def write(self, event: str, payload: dict[str, object]) -> None:
        self.rows.append((event, payload))


class DeadSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_external_close_publishes_stopped_state() -> None:
    updates = []
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.profile = type("Profile", (), {"id": "farm-1"})()
    worker.on_update = updates.append
    worker.event_log = FakeLog()
    worker._farm_roster = ()
    session = DeadSession()
    worker.session = session

    worker._handle_external_close()

    assert session.closed is True
    assert worker.session is None
    assert updates[-1].state.value == "stopped"
    assert updates[-1].message == "Cửa sổ Chrome đã đóng"
