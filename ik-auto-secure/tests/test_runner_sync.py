from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

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
    runner._sync_lock = threading.Lock()
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


def test_sync_ignores_non_master_event() -> None:
    runner = make_runner()

    runner._on_input("follower-open", {"type": "pointerdown"})

    assert all(not worker.commands for worker in runner.workers.values())


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
