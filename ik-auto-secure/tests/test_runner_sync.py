from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

import ik_chrome_auto.runner as runner_module
from ik_chrome_auto.models import Auto2048Speed, CommandKind, WorkerCommand
from ik_chrome_auto.runner import (
    AUTO_2048_TARGET_LEVEL,
    AUTO_2048_TIMINGS,
    MultiProfileRunner,
    ProfileWorker,
)
from ik_chrome_auto.windows import ProcessResourceUsage


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


def test_2048_speed_modes_are_ordered_from_safe_to_turbo() -> None:
    delays = [
        AUTO_2048_TIMINGS[speed].move_delay_seconds
        for speed in (
            Auto2048Speed.SAFE,
            Auto2048Speed.BALANCED,
            Auto2048Speed.FAST,
            Auto2048Speed.TURBO,
        )
    ]
    assert delays == sorted(delays, reverse=True)
    assert delays == [1.20, 0.80, 0.55, 0.35]


def test_global_2048_speed_routes_to_every_worker() -> None:
    runner = make_runner()
    runner.config = type(
        "Config",
        (),
        {
            "auto_2048_speed": Auto2048Speed.BALANCED,
            "profiles": [
                type("Profile", (), {"id": "master"})(),
                type("Profile", (), {"id": "follower-open"})(),
                type("Profile", (), {"id": "follower-closed"})(),
            ],
        },
    )()

    runner.set_auto_2048_speed(Auto2048Speed.FAST)

    assert runner.auto_2048_speed == Auto2048Speed.FAST
    assert runner.config.auto_2048_speed == Auto2048Speed.FAST
    assert all(
        worker.commands[-1].kind == CommandKind.SET_2048_SPEED
        and worker.commands[-1].payload == {"speed": "fast"}
        for worker in runner.workers.values()
    )


def test_auto_2048_stops_before_swipe_when_level_12_is_reached() -> None:
    class Session:
        def __init__(self) -> None:
            self.swipes = 0

        def capture_game_surface_png(self):
            return b"png", {}

        def swipe_game_surface(self, *_args, **_kwargs) -> None:
            self.swipes += 1

    board = (
        (12, 1, 0, 0),
        (2, 3, 4, 0),
        (5, 6, 7, 0),
        (8, 9, 10, 11),
    )
    scan = SimpleNamespace(
        board=board,
        grid=SimpleNamespace(box={}),
        image_width=500,
        image_height=300,
        confidence=1.0,
    )
    player = SimpleNamespace(
        plan=lambda _png: SimpleNamespace(
            scan=scan,
            direction="left",
            depth=3,
            waiting=False,
        ),
        stale_retries=0,
    )
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = Session()
    worker._auto_2048 = player
    worker._auto_2048_speed = Auto2048Speed.BALANCED
    worker._auto_2048_errors = 0
    updates: list[tuple[object, str, str]] = []
    worker._publish = lambda state, message, detail="": updates.append(
        (state, message, detail)
    )

    worker._run_2048_tick()

    assert AUTO_2048_TARGET_LEVEL == 12
    assert worker._auto_2048 is None
    assert worker.session.swipes == 0
    assert "đạt level 12" in updates[-1][1]


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
    session = DeadSession()
    worker.session = session

    worker._handle_external_close()

    assert session.closed is True
    assert worker.session is None
    assert updates[-1].state.value == "stopped"
    assert updates[-1].message == "Cửa sổ Chrome đã đóng"
