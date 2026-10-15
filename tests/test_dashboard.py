from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import ik_chrome_auto.dashboard as dashboard_module
from ik_chrome_auto.dashboard import (
    TAB_CLOSE_BATCH_PAUSE_SECONDS,
    TAB_CLOSE_INTERVAL_SECONDS,
    Dashboard,
    FarmProfileDialog,
    ProfileRow,
)
from ik_chrome_auto.farm_launch_policy import FarmLaunchPolicy
from ik_chrome_auto.mail_monitor import (
    COMBAT_MAIL_OTHER,
    MAIL_BASELINE,
    NO_NEW_COMBAT_MAIL,
    SCAN_ERROR,
    TERRITORY_ATTACKED,
)
from ik_chrome_auto.models import CommandKind, WorkerSnapshot, WorkerState


class FakeLogWidget:
    def __init__(self) -> None:
        self.value = ""

    def configure(self, **_kwargs) -> None:
        return None

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""

    def insert(self, _position: str, value: str) -> None:
        self.value += value

    def see(self, _position: str) -> None:
        return None


class FakeCollapsibleWidget:
    def __init__(self) -> None:
        self.visible = False

    def setVisible(self, visible: bool) -> None:
        self.visible = visible


class FakeToggleButton:
    def __init__(self) -> None:
        self.icon = None
        self.tooltip = ""

    def setIcon(self, icon) -> None:
        self.icon = icon

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip


def test_recover_offscreen_dashboard_after_monitor_change() -> None:
    from ik_chrome_auto.dashboard import _window_is_effectively_offscreen
    screen = [(0, 0, 1920, 1080)]
    assert _window_is_effectively_offscreen((-781, 385, 789, 663), screen)
    assert _window_is_effectively_offscreen((1930, 385, 789, 663), screen)
    assert not _window_is_effectively_offscreen((1100, 385, 789, 663), screen)
    assert not _window_is_effectively_offscreen((0, 0, 789, 663), screen)
    assert not _window_is_effectively_offscreen((1921, 100, 789, 663), [(1920, 0, 1920, 1080)])


def test_cdp_health_count_drops_to_zero_after_all_profiles_close() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    live = {"a", "b"}
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=pid, enabled=True) for pid in ("a", "b")]
    )
    dashboard.runner = SimpleNamespace(has_open_session=lambda pid: pid in live)
    dashboard.reconnect_health = FakeProfileOpenButton()
    dashboard._reattach_pending_profiles = set()

    dashboard._update_reconnect_health()
    assert dashboard.reconnect_health.text.startswith("CDP 2/2")
    live.clear()
    dashboard._update_reconnect_health()
    assert dashboard.reconnect_health.text.startswith("CDP 0/2")


def test_resource_refresh_also_refreshes_cdp_label() -> None:
    from pathlib import Path
    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "ik_chrome_auto" / "dashboard.py"
    ).read_text(encoding="utf-8")
    body = source.split("def _drain_resource_overview(self)", 1)[1].split(
        "def _trim_profile_memory_safely", 1
    )[0]
    assert "self._update_reconnect_health()" in body


def test_profile_window_toggle_updates_icon_tooltip_and_log() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.runner = SimpleNamespace(
        toggle_all_profile_windows=lambda: (True, 3)
    )
    dashboard.window_visibility = FakeToggleButton()
    logs: list[str] = []
    dashboard._append_log = logs.append

    dashboard._toggle_profile_windows()

    assert dashboard.window_visibility.tooltip == "Mở tất cả cửa sổ từ taskbar"
    assert "thu gọn 3 cửa sổ" in logs[-1]


class FakeStatusIcon:
    def __init__(self) -> None:
        self.style = ""
        self.tooltip = ""

    def setStyleSheet(self, style: str) -> None:
        self.style = style

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip


class FakeProfileCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, object, object]] = []

    def addItem(self, text: str, icon=None, userData=None) -> None:
        self.items.append((text, icon, userData))


class FakeActionButton:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = False
        self.style = ""
        self.icon = None

    def setText(self, text: str) -> None:
        self.text = text

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setStyleSheet(self, style: str) -> None:
        self.style = style

    def setIcon(self, icon) -> None:
        self.icon = icon


class FakeProfileOpenButton(FakeActionButton):
    def __init__(self) -> None:
        super().__init__()
        self.visible = True

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip


def test_open_closed_profile_submits_one_open_command_and_disables_its_button() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    button = FakeProfileOpenButton()
    row = SimpleNamespace(open_button=button)
    commands: list[tuple[str, CommandKind]] = []
    dashboard.rows = {"account-2": row}
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda _profile_id: False,
        submit=lambda profile_id, command: commands.append((profile_id, command)),
    )
    dashboard._append_log = lambda _message: None
    dashboard._error = lambda _title, _message: None

    dashboard._open_profile("account-2")

    assert commands == [("account-2", CommandKind.OPEN)]
    assert button.visible
    assert not button.enabled
    assert button.text == "Đang mở…"


def test_open_profile_does_not_repeat_command_for_an_open_session() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    button = FakeProfileOpenButton()
    dashboard.rows = {"account-2": SimpleNamespace(open_button=button)}
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda _profile_id: True,
        submit=lambda *_args: (_ for _ in ()).throw(AssertionError("must not submit")),
    )

    dashboard._open_profile("account-2")

    assert button.visible
    assert not button.enabled
    assert button.text == "Đã mở"


def test_open_profile_rejects_second_click_before_worker_attaches() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    button = FakeProfileOpenButton()
    submitted: list[tuple[str, CommandKind]] = []
    dashboard.rows = {"account-2": SimpleNamespace(open_button=button)}
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda _pid: False,
        submit=lambda pid, cmd: submitted.append((pid, cmd)),
    )
    dashboard._append_log = lambda _message: None
    dashboard._error = lambda _title, _message: None

    dashboard._open_profile("account-2")
    dashboard._open_profile("account-2")

    assert submitted == [("account-2", CommandKind.OPEN)]
    assert not button.enabled


def test_detected_chrome_disables_open_even_without_cdp() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    button = FakeProfileOpenButton()
    dashboard.rows = {"account-2": SimpleNamespace(open_button=button)}
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda _pid: False,
        is_browser_running=lambda _pid: False,
        submit=lambda *_args: (_ for _ in ()).throw(AssertionError("must not submit")),
    )
    dashboard._chrome_process_results = __import__("queue").Queue(maxsize=1)
    dashboard._chrome_process_results.put({"account-2"})
    dashboard._chrome_process_sample_in_flight = True
    dashboard._detected_open_profiles = set()
    dashboard._chrome_process_probe_ok = False

    dashboard._drain_chrome_process_sample()
    dashboard._open_profile("account-2")

    assert button.visible and not button.enabled
    assert button.text == "Đã mở"


def test_profile_grid_reflows_to_one_column_on_narrow_window() -> None:
    class FakeGrid:
        def __init__(self) -> None:
            self.positions = {}

        def count(self) -> int:
            return len(self.positions)

        def setColumnStretch(self, _col: int, _factor: int) -> None:
            pass

        def removeWidget(self, card) -> None:
            self.positions.pop(card, None)

        def addWidget(self, card, row: int, col: int) -> None:
            self.positions[card] = (row, col)

    dashboard = Dashboard.__new__(Dashboard)
    width = [720]
    dashboard.scroll = SimpleNamespace(viewport=lambda: SimpleNamespace(width=lambda: width[0]))
    dashboard.table_layout = FakeGrid()
    dashboard.rows = {
        name: SimpleNamespace(card=name)
        for name in ("one", "two", "three", "four")
    }
    dashboard._profile_columns = 1
    dashboard._profile_reflow_scheduled = True

    dashboard._layout_profile_cards()
    assert dashboard._profile_columns == 2
    assert dashboard.table_layout.positions["two"] == (0, 1)
    assert dashboard.table_layout.positions["three"] == (1, 0)

    width[0] = 450
    dashboard._layout_profile_cards()
    assert dashboard._profile_columns == 1
    assert dashboard.table_layout.positions["two"] == (1, 0)
    assert dashboard.table_layout.positions["four"] == (3, 0)


def test_profile_controls_show_close_only_for_attached_chrome() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._closing_profiles = set()
    dashboard._farm_launcher_phase = "launch"
    dashboard.runner = SimpleNamespace(has_open_session=lambda pid: pid == "connected")
    opened = FakeProfileOpenButton()
    close = FakeProfileOpenButton()
    row = SimpleNamespace(open_button=opened, close_button=close, profile_id="connected")

    dashboard._set_profile_open_control(row, opened=True)
    assert not opened.enabled
    assert close.enabled
    assert close.text == "Đóng"

    row.profile_id = "detached"
    dashboard._set_profile_open_control(row, opened=True)
    assert not opened.enabled
    assert not close.enabled
    assert "Retry CDP" in close.tooltip


def test_close_one_profile_submits_only_one_stop_and_disables_repeat() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    commands = []
    button = FakeProfileOpenButton()
    close = FakeProfileOpenButton()
    dashboard.rows = {
        "one": SimpleNamespace(open_button=button, close_button=close, profile_id="one")
    }
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda pid: pid in {"one", "two"},
        sync_enabled=False,
        submit=lambda pid, kind: commands.append((pid, kind)),
    )
    dashboard._farm_launcher_phase = "launch"
    dashboard._closing_profiles = set()
    dashboard.farm_profiles = {"one", "two"}
    dashboard._farm_launch_profiles = {"one", "two"}
    dashboard._farm_open_queue = deque()
    dashboard._monitor_queue = deque(["one", "two"])
    dashboard._monitor_batch_pending = deque(["one", "two"])
    dashboard._append_log = lambda _msg: None
    dashboard._log_monitor_scheduler = lambda *_args, **_kwargs: None
    dashboard._close_profile("one")
    dashboard._close_profile("one")

    assert commands == [("one", CommandKind.STOP)]
    assert not close.enabled
    assert close.text == "Đang đóng…"
    assert dashboard.farm_profiles == {"two"}
    assert list(dashboard._monitor_queue) == ["two"]


def test_close_detached_profile_never_pretends_to_terminate_chrome() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.rows = {"one": SimpleNamespace(open_button=FakeProfileOpenButton())}
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda _pid: False,
        is_browser_running=lambda _pid: True,
        submit=lambda *_args: (_ for _ in ()).throw(AssertionError("must not STOP")),
    )
    dashboard._farm_launcher_phase = "launch"
    dashboard._closing_profiles = set()
    warnings = []
    dashboard._warning = lambda title, description: warnings.append((title, description))

    dashboard._close_profile("one")

    assert len(warnings) == 1
    assert "Retry CDP" in warnings[0][1]


def test_close_all_retained_profiles_uses_existing_serial_shutdown_after_confirmation(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(profiles=[SimpleNamespace(id="one"), SimpleNamespace(id="two")])
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda pid: pid in {"one", "two"},
        is_browser_running=lambda _pid: False,
    )
    dashboard._farm_launcher_phase = "launch"
    calls = []
    dashboard._farm_launcher_action = lambda: calls.append(dashboard._farm_launcher_phase)
    dashboard._refresh_close_all_control = lambda: None
    monkeypatch.setattr(
        dashboard_module.QMessageBox, "question",
        lambda *_args: dashboard_module.QMessageBox.StandardButton.Yes,
    )

    dashboard._close_all_profiles()

    assert calls == ["ready"]


def test_close_all_cancel_never_sends_stop(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(profiles=[SimpleNamespace(id="one")])
    dashboard.runner = SimpleNamespace(has_open_session=lambda _pid: True)
    dashboard._farm_launcher_phase = "launch"
    dashboard._farm_launcher_action = lambda: (_ for _ in ()).throw(AssertionError("must not close"))
    monkeypatch.setattr(
        dashboard_module.QMessageBox, "question",
        lambda *_args: dashboard_module.QMessageBox.StandardButton.No,
    )
    dashboard._close_all_profiles()
    assert dashboard._farm_launcher_phase == "launch"


def test_profile_reattach_disables_launch_until_every_profile_settles() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.runner = SimpleNamespace(
        reattach_existing_profiles=lambda: {"account-1", "account-2"}
    )
    dashboard.farm_launcher = FakeActionButton()
    dashboard._farm_launcher_phase = "launch"
    dashboard._append_log = lambda _message: None
    refreshes: list[bool] = []
    dashboard._refresh_sync_control = lambda: refreshes.append(True)

    dashboard._begin_profile_reattach()

    assert dashboard.farm_launcher.enabled is False
    assert dashboard.farm_launcher.text == "Đang kết nối…"

    dashboard._settle_profile_reattach(
        WorkerSnapshot("account-1", WorkerState.READY, "Đã kết nối")
    )
    assert dashboard.farm_launcher.enabled is False

    dashboard._settle_profile_reattach(
        WorkerSnapshot("account-2", WorkerState.STOPPED, "Profile đang đóng")
    )
    assert dashboard.farm_launcher.enabled is True
    assert dashboard.farm_launcher.text == "Khởi động"
    assert refreshes == [True]


def test_individually_opened_profile_joins_active_tools() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    sync_calls: list[tuple[str, set[str]]] = []
    added_sync_targets: list[str] = []
    commands: list[tuple[str, CommandKind]] = []
    monitor_calls: list[set[str]] = []
    restore_calls: list[set[str]] = []
    dashboard.runner = SimpleNamespace(
        sync_enabled=True,
        sync_master_id="account-1",
        sync_target_ids={"account-2"},
        enable_sync=lambda master_id, targets: sync_calls.append((master_id, set(targets))),
        add_sync_target=lambda profile_id: added_sync_targets.append(profile_id) or True,
        submit=lambda profile_id, command: commands.append((profile_id, command)),
        enable_mail_monitor=lambda profile_ids: monitor_calls.append(set(profile_ids)),
        restore_profile_windows=lambda profile_ids: restore_calls.append(set(profile_ids)),
    )
    dashboard._sync_available_profiles = {"account-2"}
    dashboard._sync_target_profiles = {"account-2"}
    dashboard.farm_profiles = set()
    dashboard._farm_all_running = True
    dashboard._monitoring_enabled = True
    dashboard._monitor_queue = deque()
    dashboard._monitor_batch_pending = deque()
    dashboard._monitor_batch_profiles = set()
    dashboard._monitor_in_flight = {}
    dashboard._append_log = lambda _message: None

    dashboard._include_opened_profile_in_active_tools("account-3")

    assert sync_calls == []
    assert added_sync_targets == ["account-3"]
    assert commands == [("account-3", CommandKind.START_FARM)]
    assert restore_calls == [{"account-3"}]
    assert monitor_calls == [{"account-3"}]
    assert list(dashboard._monitor_queue) == ["account-3"]


def test_sync_control_stays_available_while_farm_is_running() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync = FakeActionButton()
    dashboard.farm_profiles = {"account-1", "account-2"}
    dashboard._monitoring_enabled = True
    dashboard._farm_launcher_phase = "ready"

    dashboard._refresh_sync_control()

    assert dashboard.sync.enabled


def test_sync_control_is_locked_only_while_tabs_are_transitioning() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync = FakeActionButton()
    dashboard.farm_profiles = set()
    dashboard._monitoring_enabled = False
    dashboard._farm_launcher_phase = "stopping"

    dashboard._refresh_sync_control()

    assert not dashboard.sync.enabled


def test_active_sync_can_always_be_turned_off_while_tabs_are_transitioning() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync = FakeActionButton()
    dashboard.runner = SimpleNamespace(sync_enabled=True)
    dashboard.farm_profiles = set()
    dashboard._farm_all_running = False
    dashboard._farm_launcher_phase = "opening"

    dashboard._refresh_sync_control()

    assert dashboard.sync.enabled


def test_enabling_sync_stops_automation_modes_first() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.farm_profiles = {"account-1"}
    dashboard._monitoring_enabled = True
    calls: list[str] = []
    logs: list[str] = []
    dashboard._stop_all_farms = lambda: calls.append("farm")
    dashboard._stop_monitoring = lambda: calls.append("monitor")
    dashboard._append_log = logs.append

    dashboard._stop_automation_for_sync()

    assert calls == ["farm", "monitor"]
    assert "AutoFarm và Giám sát" in logs[-1]


class FakeFarmRunner:
    def __init__(self, opened: set[str]) -> None:
        self.opened = opened
        self.commands: list[tuple[str, CommandKind]] = []
        self.sync_enabled = False
        self.disable_sync_calls = 0
        self.restore_calls: list[set[str] | None] = []

    def has_open_session(self, profile_id: str) -> bool:
        return profile_id in self.opened

    def submit(self, profile_id: str, command: CommandKind) -> None:
        self.commands.append((profile_id, command))

    def disable_sync(self) -> None:
        self.sync_enabled = False
        self.disable_sync_calls += 1

    def cancel_mail_monitor(self) -> None:
        self.mail_monitor_cancelled = True

    def restore_profile_windows(self, profile_ids: set[str] | None = None) -> int:
        self.restore_calls.append(None if profile_ids is None else set(profile_ids))
        return 0


class FakeTelegramNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str) -> bool:
        self.messages.append(message)
        return True


class FakeMonitorRunner:
    def __init__(self, opened: set[str]) -> None:
        self.opened = opened
        self.commands: list[tuple[str, CommandKind, dict[str, object]]] = []

    def has_open_session(self, profile_id: str) -> bool:
        return profile_id in self.opened

    def submit(self, profile_id: str, command: CommandKind, **payload: object) -> None:
        self.commands.append((profile_id, command, payload))


def test_dashboard_log_keeps_only_ten_latest_rows() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    for index in range(15):
        dashboard._append_log(f"row-{index}")

    assert dashboard.log.value.splitlines() == [f"row-{index}" for index in range(5, 15)]


def test_hidden_dashboard_log_retains_history_without_rendering() -> None:
    class HiddenLog:
        rendered = 0

        @staticmethod
        def isVisible() -> bool:
            return False

        def setPlainText(self, _value: str) -> None:
            self.rendered += 1

    dashboard = Dashboard.__new__(Dashboard)
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = HiddenLog()

    dashboard._append_log("profile update")

    assert list(dashboard._log_lines) == ["profile update"]
    assert dashboard.log.rendered == 0


def test_repeated_profile_snapshot_does_not_repaint_unchanged_widgets(monkeypatch) -> None:
    class CountingLabel:
        def __init__(self) -> None:
            self.text_calls = 0
            self.style_calls = 0

        def setText(self, _value: str) -> None:
            self.text_calls += 1

        def setStyleSheet(self, _value: str) -> None:
            self.style_calls += 1

    dashboard = Dashboard.__new__(Dashboard)
    status, roster, resource, badge = (CountingLabel() for _ in range(4))
    card = object()
    row = ProfileRow(status, roster, resource, badge, card)
    card_state_calls: list[WorkerState] = []
    roster_calls: list[tuple[tuple[int, str], ...]] = []
    monkeypatch.setattr(
        Dashboard,
        "_set_profile_card_state",
        staticmethod(lambda _card, state: card_state_calls.append(state)),
    )
    monkeypatch.setattr(
        Dashboard,
        "_set_roster_tooltip",
        staticmethod(lambda _card, roster_value: roster_calls.append(roster_value)),
    )
    monkeypatch.setattr(
        Dashboard,
        "_set_roster_dots",
        staticmethod(lambda _label, _roster: None),
    )
    snapshot = WorkerSnapshot(
        "account-1",
        WorkerState.RUNNING,
        "Đang farm",
        farm_roster=((1, "ready"), (2, "busy")),
    )

    dashboard._apply_profile_snapshot(row, snapshot)
    dashboard._apply_profile_snapshot(row, snapshot)

    assert status.text_calls == 1
    assert badge.text_calls == 1
    assert badge.style_calls == 1
    assert card_state_calls == [WorkerState.RUNNING]
    assert roster_calls == [snapshot.farm_roster]


def test_roster_dots_show_each_team_with_its_current_status_color() -> None:
    dots = Dashboard._roster_dot_html(((1, "busy"), (2, "ready"), (3, "busy")))

    assert dots.count("&#9679;") == 3
    assert dots.count("#f59e0b") == 2
    assert dots.count("#16a34a") == 1


def test_labeled_action_state_style_keeps_base_icon_spacing() -> None:
    button = FakeActionButton()
    button._ik_labeled_base_style = "QPushButton[hasIcon=true] { padding-left: 36px; }"

    Dashboard._set_labeled_action_style(
        button,
        "QPushButton { background: #2563eb; }",
    )

    assert "padding-left: 36px" in button.style
    assert "background: #2563eb" in button.style

    Dashboard._set_labeled_action_style(button)
    assert button.style == button._ik_labeled_base_style


def test_monitor_first_pass_builds_baseline_then_alerts_only_for_attack_mail() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profile=lambda _profile_id: SimpleNamespace(name="Tài khoản 01 · cuongg********")
    )
    dashboard._telegram_notifier = FakeTelegramNotifier()
    dashboard._telegram_event_at = {}
    dashboard._monitor_initialized_profiles = set()
    dashboard._monitor_queue = deque()
    dashboard._monitor_in_flight = {"account-1": 1.0}
    dashboard._monitor_cycle_at = 0.0
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    baseline = WorkerSnapshot(
        "account-1",
        monitor_events=(MAIL_BASELINE,),
        monitor_checked=("combat_mail",),
    )
    dashboard._handle_monitor_result(baseline)
    assert dashboard._telegram_notifier.messages == []
    assert dashboard._monitor_initialized_profiles == {"account-1"}

    dashboard._monitor_in_flight = {"account-1": 1.0}
    dashboard._handle_monitor_result(
        WorkerSnapshot("account-1", monitor_events=(COMBAT_MAIL_OTHER,))
    )
    assert dashboard._telegram_notifier.messages == []

    dashboard._monitor_in_flight = {"account-1": 1.0}
    dashboard._handle_monitor_result(
        WorkerSnapshot("account-1", monitor_events=(TERRITORY_ATTACKED,))
    )
    assert len(dashboard._telegram_notifier.messages) == 1
    assert "Lãnh Địa bị Công" in dashboard._telegram_notifier.messages[0]


def test_monitor_completes_each_profile_flow_before_dispatching_the_next() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    profiles = [SimpleNamespace(id=f"account-{index}") for index in range(7)]
    dashboard.config = SimpleNamespace(
        profiles=profiles,
        profile=lambda profile_id: SimpleNamespace(name=profile_id),
    )
    dashboard.runner = FakeMonitorRunner({profile.id for profile in profiles})
    dashboard._monitoring_enabled = True
    dashboard._monitor_queue = deque()
    dashboard._monitor_in_flight = {}
    dashboard._monitor_batch_profiles = set()
    dashboard._monitor_batch_pending = deque()
    dashboard._monitor_batch_members = ()
    dashboard._monitor_batch_phase = ""
    dashboard._monitor_next_profile_at = 0.0
    dashboard._monitor_next_batch_at = 0.0
    dashboard._monitor_cycle_at = 0.0
    dashboard._monitor_cycle_number = 0
    dashboard._monitor_initialized_profiles = set()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    dashboard._advance_monitoring()

    # The group is fixed at five, but it is deliberately serial: profile 1
    # owns the renderer until it publishes the completed baseline result.
    assert len(dashboard.runner.commands) == 1
    assert len(dashboard._monitor_in_flight) == 1
    assert len(dashboard._monitor_batch_pending) == 4
    assert len(dashboard._monitor_batch_profiles) == 5

    for _ in range(4):
        dashboard._monitor_next_profile_at = 0.0
        dashboard._advance_monitoring()

    assert len(dashboard.runner.commands) == 1
    assert list(dashboard._monitor_in_flight) == ["account-0"]

    # Completing each profile is the only operation which unlocks the next
    # profile in the same five-member group.
    for profile_id in ("account-0", "account-1", "account-2", "account-3"):
        dashboard._handle_monitor_result(
            WorkerSnapshot(profile_id, monitor_events=(MAIL_BASELINE,))
        )
        dashboard._monitor_next_profile_at = 0.0
        dashboard._advance_monitoring()
        assert dashboard.runner.commands[-1] == (
            f"account-{int(profile_id.rsplit('-', 1)[1]) + 1}",
            CommandKind.MONITOR_MAIL,
            {"initial_scan": True},
        )

    dashboard._handle_monitor_result(
        WorkerSnapshot("account-4", monitor_events=(MAIL_BASELINE,))
    )
    # The first five profiles do not begin Lượt 2 yet: baseline must also
    # finish for accounts 5 and 6 before the first combat scan is eligible.
    assert dashboard._monitor_batch_phase == ""
    dashboard._monitor_next_batch_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1] == (
        "account-5", CommandKind.MONITOR_MAIL, {"initial_scan": True}
    )
    dashboard._handle_monitor_result(
        WorkerSnapshot("account-5", monitor_events=(MAIL_BASELINE,))
    )
    dashboard._monitor_next_profile_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1] == (
        "account-6", CommandKind.MONITOR_MAIL, {"initial_scan": True}
    )
    dashboard._handle_monitor_result(
        WorkerSnapshot("account-6", monitor_events=(MAIL_BASELINE,))
    )
    assert dashboard._monitor_batch_phase == "combat"
    assert list(dashboard._monitor_batch_pending) == [
        "account-0", "account-1", "account-2", "account-3", "account-4", "account-5", "account-6"
    ]

    # Only now may Lượt 2 repeat over the whole initialized fleet.
    dashboard._monitor_next_profile_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1] == (
        "account-0", CommandKind.MONITOR_MAIL, {"initial_scan": False}
    )
    for profile_id in ("account-0", "account-1", "account-2", "account-3", "account-4", "account-5"):
        dashboard._handle_monitor_result(
            WorkerSnapshot(profile_id, monitor_events=(NO_NEW_COMBAT_MAIL,))
        )
        dashboard._monitor_next_profile_at = 0.0
        dashboard._advance_monitoring()
        assert dashboard.runner.commands[-1] == (
            f"account-{int(profile_id.rsplit('-', 1)[1]) + 1}",
            CommandKind.MONITOR_MAIL,
            {"initial_scan": False},
        )
    dashboard._handle_monitor_result(WorkerSnapshot("account-6", monitor_events=(NO_NEW_COMBAT_MAIL,)))
    dashboard._monitor_next_batch_at = 0.0
    dashboard._monitor_cycle_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1] == (
        "account-0", CommandKind.MONITOR_MAIL, {"initial_scan": False}
    )


def test_monitor_retries_the_same_profile_after_a_baseline_error() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    profiles = [SimpleNamespace(id=f"account-{index}") for index in range(2)]
    dashboard.config = SimpleNamespace(
        profiles=profiles,
        profile=lambda profile_id: SimpleNamespace(name=profile_id),
    )
    dashboard.runner = FakeMonitorRunner({profile.id for profile in profiles})
    dashboard._monitoring_enabled = True
    dashboard._monitor_queue = deque()
    dashboard._monitor_in_flight = {}
    dashboard._monitor_batch_profiles = set()
    dashboard._monitor_batch_pending = deque()
    dashboard._monitor_batch_members = ()
    dashboard._monitor_batch_phase = ""
    dashboard._monitor_next_profile_at = 0.0
    dashboard._monitor_next_batch_at = 0.0
    dashboard._monitor_cycle_at = 0.0
    dashboard._monitor_cycle_number = 0
    dashboard._monitor_initialized_profiles = set()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    dashboard._advance_monitoring()
    assert dashboard.runner.commands == [
        ("account-0", CommandKind.MONITOR_MAIL, {"initial_scan": True})
    ]

    dashboard._handle_monitor_result(
        WorkerSnapshot("account-0", monitor_events=(SCAN_ERROR,))
    )

    assert dashboard._monitor_in_flight == {}
    assert list(dashboard._monitor_batch_pending) == ["account-0", "account-1"]
    assert dashboard._monitor_batch_profiles == {"account-0", "account-1"}
    dashboard._monitor_next_profile_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1] == (
        "account-0", CommandKind.MONITOR_MAIL, {"initial_scan": True}
    )


def test_mouse_sync_section_is_collapsed_by_default_and_can_toggle() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync_section_expanded = False
    dashboard.sync_section = FakeCollapsibleWidget()
    dashboard.sync_section_toggle = FakeToggleButton()

    dashboard._toggle_sync_section()
    assert dashboard.sync_section_expanded is True
    assert dashboard.sync_section.visible is True
    assert dashboard.sync_section_toggle.tooltip == "Thu gọn Đồng bộ chuột - bàn phím"

    dashboard._toggle_sync_section()
    assert dashboard.sync_section_expanded is False
    assert dashboard.sync_section.visible is False
    assert dashboard.sync_section_toggle.tooltip == "Mở rộng Đồng bộ chuột - bàn phím"


def test_mouse_sync_status_indicator_changes_colour_and_tooltip() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync_status_icon = FakeStatusIcon()

    dashboard._set_sync_status_indicator(False)
    assert "#94a3b8" in dashboard.sync_status_icon.style
    assert "font-size:26px" in dashboard.sync_status_icon.style
    assert dashboard.sync_status_icon.tooltip == "Đồng bộ chuột - bàn phím đang tắt"

    dashboard._set_sync_status_indicator(True)
    assert "#16a34a" in dashboard.sync_status_icon.style
    assert dashboard.sync_status_icon.tooltip == "Đồng bộ chuột - bàn phím đang bật"


def test_sync_master_profile_id_is_stored_as_combo_user_data() -> None:
    combo = FakeProfileCombo()

    Dashboard._add_master_profile_option(combo, "cuongg********", "account-2")

    assert combo.items == [("cuongg********", None, "account-2")]


def test_newly_opened_profile_group_defaults_sync_to_every_follower() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._sync_available_profiles = {"account-2", "account-3"}
    dashboard._sync_target_profiles = {"account-2"}

    selected = dashboard._default_sync_targets(
        {f"account-{index}" for index in range(2, 46)}
    )

    assert len(selected) == 44


def test_unchanged_profile_group_preserves_manual_sync_subset() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._sync_available_profiles = {"account-2", "account-3", "account-4"}
    dashboard._sync_target_profiles = {"account-2", "account-4"}

    selected = dashboard._default_sync_targets(
        {"account-2", "account-3", "account-4"}
    )

    assert selected == {"account-2", "account-4"}


def test_dashboard_uses_three_fifths_of_every_screen_height() -> None:
    compact = Dashboard._responsive_metrics(1366, 768)
    desktop = Dashboard._responsive_metrics(1920, 1080)

    assert compact == (792, 461, 246, 285, True)
    assert desktop == (773, 648, 240, 279, False)


def test_dashboard_never_exceeds_small_logical_screen_geometry() -> None:
    width, height, sidebar_min, sidebar_max, compact = Dashboard._responsive_metrics(640, 400)

    assert (width, height) == (502, 240)
    assert sidebar_min <= sidebar_max < width
    assert compact is True


def test_dashboard_uses_smaller_typography_on_high_dpi_logical_screen() -> None:
    dense = Dashboard._responsive_typography(960, 540)
    compact = Dashboard._responsive_typography(1366, 768)
    desktop = Dashboard._responsive_typography(1920, 1080)

    assert dense == (5.84, 5.64, 9.66, 5.23, 7.45, 24)
    assert dense[0] < compact[0] < desktop[0]
    assert dense[-1] < compact[-1] < desktop[-1]


def test_profile_username_preview_is_limited_to_nine_characters() -> None:
    assert Dashboard._mask_username("cuongg2003") == "cuongg***..."
    assert Dashboard._mask_username("nam139abc") == "nam139***"


def test_startup_profile_rows_are_batched_before_cdp_reattach(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    profiles = [SimpleNamespace(id=f"account-{n}") for n in range(11)]
    dashboard.config = SimpleNamespace(profiles=profiles, source=Path("config.json"))
    dashboard._initial_profile_row_cursor = 0
    created: list[int] = []
    finalized: list[bool] = []
    attached: list[bool] = []
    callbacks: list[object] = []
    dashboard._add_profile_row = lambda profile, number: created.append(number)
    dashboard._finalize_profile_rows = lambda: finalized.append(True)
    dashboard._begin_profile_reattach = lambda: attached.append(True)
    monkeypatch.setattr(dashboard_module.QTimer, "singleShot", lambda ms, fn: callbacks.append(fn))
    monkeypatch.setattr(dashboard_module, "record", lambda *_args, **_kwargs: None)

    dashboard._append_initial_profile_row_batch()
    assert created == [1, 2, 3, 4]
    assert not finalized and not attached
    callbacks.pop(0)()
    assert created == list(range(1, 9))
    callbacks.pop(0)()
    assert created == list(range(1, 12))
    assert finalized and not attached
    callbacks.pop(0)()
    assert attached == [True]


def test_farm_profile_picker_uses_two_columns_only_above_ten_profiles() -> None:
    assert FarmProfileDialog._column_count(1) == 1
    assert FarmProfileDialog._column_count(10) == 1
    assert FarmProfileDialog._column_count(11) == 2
    assert FarmProfileDialog._column_count(50) == 2


def test_farm_profile_picker_uses_one_button_to_toggle_all_profiles() -> None:
    class FakeCheck:
        def __init__(self, checked: bool, enabled: bool = True) -> None:
            self.checked = checked
            self.enabled = enabled

        def isChecked(self) -> bool:
            return self.checked

        def isEnabled(self) -> bool:
            return self.enabled

        def setChecked(self, checked: bool) -> None:
            self.checked = checked

    dialog = FarmProfileDialog.__new__(FarmProfileDialog)
    dialog._checks = {"one": FakeCheck(False), "two": FakeCheck(True)}
    dialog.selection_toggle = FakeActionButton()

    dialog._refresh_selection_toggle()
    assert dialog.selection_toggle.text == "Chọn tất cả"

    dialog._toggle_all_profiles()
    assert all(check.isChecked() for check in dialog._checks.values())
    assert dialog.selection_toggle.text == "Bỏ chọn"

    dialog._toggle_all_profiles()
    assert not any(check.isChecked() for check in dialog._checks.values())
    assert dialog.selection_toggle.text == "Chọn tất cả"


def test_farm_profile_picker_never_reselects_running_chrome() -> None:
    class FakeCheck:
        def __init__(self, enabled: bool) -> None:
            self.enabled = enabled
            self.checked = False

        def isEnabled(self) -> bool:
            return self.enabled

        def isChecked(self) -> bool:
            return self.checked

        def setChecked(self, checked: bool) -> None:
            self.checked = checked

    dialog = FarmProfileDialog.__new__(FarmProfileDialog)
    dialog._checks = {
        "already-open": FakeCheck(False),
        "closed-one": FakeCheck(True),
        "closed-two": FakeCheck(True),
    }
    dialog.selection_toggle = FakeActionButton()

    dialog._toggle_all_profiles()
    assert dialog.selected_profile_ids() == {"closed-one", "closed-two"}
    assert not dialog._checks["already-open"].isChecked()

    dialog._toggle_all_profiles()
    assert dialog.selected_profile_ids() == set()
    assert not dialog._checks["already-open"].isChecked()


def test_bulk_farm_button_starts_and_stops_without_closing_open_tabs() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="account-1"), SimpleNamespace(id="account-2")]
    )
    dashboard.runner = FakeFarmRunner({"account-1", "account-2"})
    dashboard.rows = {}
    dashboard.farm_profiles = set()
    dashboard._farm_all_running = False
    dashboard._farm_launcher_phase = "ready"
    dashboard.farm_all_button = FakeActionButton()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()
    dashboard.runner.sync_enabled = False

    dashboard._farm_all_action()

    assert dashboard.runner.sync_enabled is False
    assert dashboard.runner.disable_sync_calls == 0
    assert dashboard._farm_all_running is True
    assert dashboard.runner.restore_calls == [{"account-1", "account-2"}]
    assert dashboard.farm_all_button.text == "Dừng Farm"
    assert dashboard.farm_all_button.icon == dashboard_module.FIF.PAUSE
    assert set(dashboard.runner.commands) == {
        ("account-1", CommandKind.START_FARM),
        ("account-2", CommandKind.START_FARM),
    }

    dashboard.runner.commands.clear()
    dashboard._farm_all_action()

    assert dashboard._farm_all_running is False
    assert dashboard.farm_all_button.text == "AutoFarms"
    assert dashboard.farm_all_button.icon == dashboard_module.FIF.LEAF
    assert set(dashboard.runner.commands) == {
        ("account-1", CommandKind.STOP_FARM),
        ("account-2", CommandKind.STOP_FARM),
    }
    assert dashboard.runner.opened == {"account-1", "account-2"}


def test_bulk_farm_cannot_silently_disable_active_profile_sync() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="account-1"), SimpleNamespace(id="account-2")]
    )
    dashboard.runner = FakeFarmRunner({"account-1", "account-2"})
    dashboard.runner.sync_enabled = True
    dashboard.rows = {}
    dashboard.farm_profiles = set()
    dashboard._farm_all_running = False
    dashboard._farm_launcher_phase = "ready"
    dashboard.farm_all_button = FakeActionButton()
    warnings = []
    dashboard._warning = lambda title, message: warnings.append((title, message))

    dashboard._farm_all_action()

    assert dashboard.runner.sync_enabled is True
    assert dashboard.runner.disable_sync_calls == 0
    assert dashboard.runner.commands == []
    assert dashboard.farm_profiles == set()
    assert warnings == [
        (
            "Đồng bộ đang bật",
            "Hãy tắt đồng bộ chuột - bàn phím trước khi chạy Auto Farm.",
        )
    ]


def test_autofarms_does_not_stop_independent_monitoring() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="account-1"), SimpleNamespace(id="account-2")]
    )
    dashboard.runner = FakeFarmRunner({"account-1", "account-2"})
    dashboard.rows = {}
    dashboard.farm_profiles = set()
    dashboard._farm_all_running = False
    dashboard._farm_launcher_phase = "ready"
    dashboard._monitoring_enabled = True
    dashboard.farm_all_button = FakeActionButton()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()
    dashboard._stop_monitoring = lambda: (_ for _ in ()).throw(AssertionError("must not stop monitoring"))

    dashboard._farm_all_action()

    assert set(dashboard.runner.commands) == {
        ("account-1", CommandKind.START_FARM),
        ("account-2", CommandKind.START_FARM),
    }


def test_monitoring_disables_input_sync_before_starting() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="account-1")]
    )
    dashboard.runner = FakeFarmRunner({"account-1"})
    dashboard.runner.sync_enabled = True
    dashboard._telegram_notifier = FakeTelegramNotifier()
    dashboard._monitoring_enabled = False
    dashboard._monitor_queue = deque()
    dashboard._monitor_in_flight = {}
    dashboard._monitor_cycle_at = 0.0
    dashboard._monitor_cycle_number = 0
    dashboard._monitor_initialized_profiles = set()
    dashboard.monitor_button = FakeActionButton()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()
    dashboard._advance_monitoring = lambda: None

    dashboard._toggle_monitoring()

    assert dashboard.runner.sync_enabled is False
    assert dashboard.runner.disable_sync_calls == 1
    assert dashboard._monitoring_enabled is True
    assert dashboard.monitor_button.text == "Dừng giám sát"
    assert dashboard.monitor_button.icon == dashboard_module.FIF.PAUSE


def test_close_tabs_stops_individual_farm_before_serial_browser_shutdown() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="account-1"), SimpleNamespace(id="account-2")]
    )
    dashboard.runner = FakeFarmRunner({"account-1", "account-2"})
    dashboard.rows = {}
    dashboard.farm_profiles = {"account-1"}
    dashboard._farm_launch_profiles = {"account-1", "account-2"}
    dashboard._farm_all_running = False
    dashboard._farm_launcher_phase = "ready"
    dashboard.farm_launcher = FakeActionButton()
    dashboard.farm_all_button = FakeActionButton()
    dashboard._farm_close_queue = deque()
    dashboard._farm_close_in_flight = None
    dashboard._farm_close_deadline = 0.0
    dashboard._farm_quiesce_farms = set()
    dashboard._farm_quiesce_monitors = set()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    dashboard._farm_launcher_action()

    assert dashboard.runner.commands[0] == ("account-1", CommandKind.STOP_FARM)
    assert len(dashboard.runner.commands) == 1
    assert dashboard._farm_launcher_phase == "quiescing"
    assert dashboard.farm_launcher.text == "Đang dừng tác vụ…"
    assert dashboard.farm_launcher.icon == dashboard_module.FIF.PAUSE
    assert dashboard.farm_all_button.enabled is False

    # Chrome must not receive STOP until the worker confirms Farm has ended.
    dashboard._farm_quiesce_farms.clear()
    dashboard._advance_farm_quiescing()

    assert dashboard.runner.commands[1] == ("account-1", CommandKind.STOP)
    assert dashboard._farm_launcher_phase == "stopping"
    assert dashboard.farm_launcher.text == "Đang đóng tab…"
    assert dashboard.farm_launcher.icon == dashboard_module.FIF.CLOSE

    dashboard._farm_close_deadline = 0.0
    dashboard._advance_farm_stopping()

    assert dashboard.runner.commands[-1] == ("account-1", CommandKind.STOP)
    assert list(dashboard._farm_close_queue) == ["account-2"]


def test_close_tabs_adds_gpu_cooldown_and_long_pause_every_five_tabs() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._farm_close_queue = deque(["remaining"])
    dashboard._farm_closed_count = 0
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    dashboard._schedule_next_tab_close(100.0)
    assert dashboard._farm_next_close_at == 100.0 + TAB_CLOSE_INTERVAL_SECONDS

    dashboard._farm_closed_count = 9
    dashboard._schedule_next_tab_close(200.0)
    assert dashboard._farm_next_close_at == 200.0 + TAB_CLOSE_BATCH_PAUSE_SECONDS


def test_username_results_update_qt_labels_on_main_poll_only() -> None:
    from queue import Queue

    dashboard = Dashboard.__new__(Dashboard)
    result = Queue(maxsize=2)
    dashboard._username_results = result
    dashboard._username_cache = {}
    updates = []
    dashboard._username_labels = {
        "account-2": (SimpleNamespace(setText=updates.append), "old", 2)
    }
    result.put({"account-2": "masked***", "removed": "x***"})

    dashboard._drain_username_results()

    assert dashboard._username_cache["account-2"] == "masked***"
    assert updates == ["Tài khoản 02 · masked***"]


def test_profile_launch_hard_pauses_when_physical_ram_is_constrained(monkeypatch) -> None:
    """Never open a new Chrome when physical RAM has already become unsafe."""
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
    dashboard.config = SimpleNamespace(profiles=[SimpleNamespace(id="account-1")])
    dashboard.runner = FakeFarmRunner(set())
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_launch_profiles = {"account-1"}
    dashboard._farm_open_queue = deque(["account-1"])
    dashboard._farm_open_states = {}
    # A slow 45-profile startup must keep advancing even after an old static
    # launch estimate would have expired.
    dashboard._farm_open_deadline = time.monotonic() - 60.0
    dashboard._farm_launch_policy = policy
    dashboard._farm_batch_profiles = set()
    dashboard._farm_batch_submitted = 0
    dashboard._farm_batch_limit = policy.batch_size
    dashboard._farm_batch_resume_at = 0.0
    dashboard._farm_next_open_at = 0.0
    dashboard._farm_resource_pause_started = 0.0
    dashboard._farm_resource_pause_reason = None
    dashboard._latest_profile_cpu_percent = 0.0
    dashboard.farm_launcher = FakeActionButton()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()
    monkeypatch.setattr(
        dashboard_module,
        "get_system_memory_status",
        lambda: SimpleNamespace(
            available_bytes=1 * 1_073_741_824,
            load_percent=94.0,
        ),
    )
    monkeypatch.setattr(dashboard_module, "get_gpu_utilization_percent", lambda: 96.0)
    # A resource-pressure test must not depend on the host's live commit.
    monkeypatch.setattr(
        dashboard_module, "get_windows_commit_snapshot",
        lambda: SimpleNamespace(critical=False),
    )

    dashboard._advance_farm_opening()

    assert dashboard.runner.commands == []
    assert list(dashboard._farm_open_queue) == ["account-1"]
    assert dashboard._farm_resource_pause_reason == "physical_ram_critical"
    assert "Chờ RAM" in dashboard.farm_launcher.text


def test_ram_pause_stays_closed_until_hysteresis_recovers(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(64 * 1024**3)
    dashboard.runner = FakeFarmRunner(set())
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_open_queue = deque(["account-31"])
    dashboard._farm_launch_profiles = {"account-31"}
    dashboard._farm_open_states = {}
    dashboard._farm_launch_policy = policy
    dashboard._farm_batch_profiles = set()
    dashboard._farm_batch_submitted = 0
    dashboard._farm_batch_limit = policy.batch_size
    dashboard._farm_batch_resume_at = 0.0
    dashboard._farm_next_open_at = 0.0
    dashboard._farm_resource_pause_started = 0.0
    dashboard._farm_resource_pause_reason = None
    dashboard._farm_ram_recovery_samples = 0
    dashboard._latest_profile_cpu_percent = 0.0
    dashboard.farm_launcher = FakeActionButton()
    dashboard._set_labeled_action_icon = lambda *_args, **_kwargs: None
    dashboard._cached_gpu_percent = lambda: None
    dashboard._append_log = lambda *_args: None
    events = []
    dashboard._log_monitor_scheduler = lambda name, **_kw: events.append(name)
    monkeypatch.setattr(
        dashboard_module, "get_windows_commit_snapshot",
        lambda: SimpleNamespace(critical=False),
    )
    memory = SimpleNamespace(available_bytes=8 * 1024**3, load_percent=88.0)
    monkeypatch.setattr(dashboard_module, "get_system_memory_status", lambda: memory)
    dashboard._advance_farm_opening()
    assert not dashboard.runner.commands
    assert dashboard._farm_resource_pause_reason == "physical_ram_critical"
    assert list(dashboard._farm_open_queue) == ["account-31"]
    assert "farm_physical_ram_pause_started" in events

    # Being just below the stop threshold is not enough to resume.
    memory.available_bytes = 17 * 1024**3
    memory.load_percent = 73.0
    dashboard._farm_next_open_at = 0.0
    dashboard._advance_farm_opening()
    assert not dashboard.runner.commands

    # Hysteresis requires >20 GiB free and memory use <=72% on a 64 GiB box.
    memory.available_bytes = 26 * 1024**3
    memory.load_percent = 59.0
    dashboard._farm_next_open_at = 0.0
    dashboard._advance_farm_opening()
    assert not dashboard.runner.commands
    assert dashboard._farm_ram_recovery_samples == 1
    dashboard._farm_next_open_at = 0.0
    dashboard._advance_farm_opening()
    assert dashboard.runner.commands == [("account-31", CommandKind.OPEN)]
    assert "farm_physical_ram_recovered" in events


def test_ram_probe_failure_fails_closed(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(64 * 1024**3)
    dashboard.runner = FakeFarmRunner(set())
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_open_queue = deque(["account-31"])
    dashboard._farm_launch_policy = policy
    dashboard._farm_batch_profiles = set()
    dashboard._farm_batch_submitted = 0
    dashboard._farm_batch_limit = policy.batch_size
    dashboard._farm_batch_resume_at = 0.0
    dashboard._farm_next_open_at = 0.0
    dashboard._farm_resource_pause_started = 0.0
    dashboard._farm_resource_pause_reason = None
    dashboard.farm_launcher = FakeActionButton()
    dashboard._set_labeled_action_icon = lambda *_args, **_kwargs: None
    dashboard._append_log = lambda *_args: None
    dashboard._log_monitor_scheduler = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        dashboard_module, "get_windows_commit_snapshot",
        lambda: SimpleNamespace(critical=False),
    )
    def fail_memory():
        raise OSError("probe failed")
    monkeypatch.setattr(dashboard_module, "get_system_memory_status", fail_memory)
    dashboard._advance_farm_opening()
    assert dashboard.runner.commands == []
    assert list(dashboard._farm_open_queue) == ["account-31"]


def test_cancel_opening_keeps_live_sessions_and_pending_worker_lock(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_open_queue = deque(["account-3", "account-4"])
    dashboard._farm_batch_profiles = {"account-1", "account-2"}
    dashboard._farm_open_states = {
        "account-1": WorkerState.READY,
        "account-2": WorkerState.STARTING,
    }
    dashboard._farm_batch_submitted = 2
    dashboard._farm_batch_resume_at = 123.0
    dashboard._farm_resource_pause_started = 100.0
    dashboard._farm_resource_pause_reason = "physical_ram_critical"
    dashboard._farm_ram_recovery_samples = 0
    dashboard._farm_launch_profiles = {"account-1", "account-2", "account-3", "account-4"}
    dashboard._individually_opening_profiles = set()
    dashboard.runner = FakeFarmRunner({"account-1"})
    dashboard.config = SimpleNamespace(profiles=[
        SimpleNamespace(id=f"account-{i}") for i in range(1, 5)
    ])
    dashboard.farm_launcher = FakeActionButton()
    actions = []
    dashboard._set_farm_opening_controls = lambda active: actions.append(active)
    dashboard._set_labeled_action_icon = lambda *_args, **_kwargs: None
    dashboard._set_farm_launcher_launch_style = lambda: None
    dashboard._refresh_close_all_control = lambda: None
    dashboard._append_log = lambda message: actions.append(message)
    dashboard._log_monitor_scheduler = (
        lambda event, **kwargs: actions.append((event, kwargs))
    )
    monkeypatch.setattr(
        dashboard_module.QMessageBox, "question",
        lambda *_args: dashboard_module.QMessageBox.StandardButton.Yes,
    )
    dashboard._cancel_farm_opening()
    assert dashboard._farm_launcher_phase == "launch"
    assert not dashboard._farm_open_queue
    assert not dashboard.runner.commands
    assert dashboard._individually_opening_profiles == {"account-2"}
    assert dashboard._farm_launch_profiles == {"account-1"}
    assert "account-3" not in dashboard._individually_opening_profiles
    assert dashboard.farm_launcher.enabled
    assert False in actions
    assert ("profile_open_queue_cancelled", {
        "cancelled_count": 2, "in_flight_count": 1
    }) in actions


def test_cancel_opening_confirmation_no_leaves_queue_unchanged(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_open_queue = deque(["account-1"])
    monkeypatch.setattr(
        dashboard_module.QMessageBox, "question",
        lambda *_args: dashboard_module.QMessageBox.StandardButton.No,
    )
    dashboard._cancel_farm_opening()
    assert dashboard._farm_launcher_phase == "opening"
    assert list(dashboard._farm_open_queue) == ["account-1"]


def test_commit_wait_reports_queue_and_alerts_only_once_after_five_minutes() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._farm_open_queue = deque(["account-41", "account-42"])
    dashboard._farm_resource_pause_started = 0.0
    dashboard._farm_resource_pause_reason = None
    dashboard._farm_commit_wait_alerted = False
    dashboard._farm_commit_wait_last_log_at = 0.0
    dashboard.farm_launcher = FakeActionButton()
    dashboard.farm_launcher.setToolTip = lambda value: None
    dashboard._set_labeled_action_icon = lambda *_args, **_kwargs: None
    logs, events, notifications = [], [], []
    dashboard._append_log = logs.append
    dashboard._log_monitor_scheduler = lambda name, **payload: events.append((name, payload))
    dashboard._notify_telegram = lambda message, **kwargs: notifications.append(message)
    critical = SimpleNamespace(usage_ratio=0.923, available_bytes=12 * 1024**3)

    dashboard._pause_farm_for_commit(critical, 1000.0)
    assert "92.3%" in dashboard.farm_launcher.text
    assert "2 tab" in dashboard.farm_launcher.text
    assert dashboard._farm_resource_pause_reason == "commit_critical"
    assert len(notifications) == 0
    dashboard._pause_farm_for_commit(critical, 1301.0)
    dashboard._pause_farm_for_commit(critical, 1361.0)
    assert len(notifications) == 1
    assert sum(name == "farm_commit_pause_alert" for name, _ in events) == 1
    assert any(name == "farm_commit_pause_started" for name, _ in events)
    assert any(name == "farm_commit_pause_heartbeat" for name, _ in events)


def test_commit_recovery_resumes_queue_without_reopening_existing_profiles(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(32 * 1024**3)
    dashboard.config = SimpleNamespace(profiles=[SimpleNamespace(id="account-42")])
    dashboard.runner = FakeFarmRunner(set())
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_open_queue = deque(["account-42"])
    dashboard._farm_launch_profiles = {"account-42"}
    dashboard._farm_open_states = {}
    dashboard._farm_launch_policy = policy
    dashboard._farm_batch_profiles = set()
    dashboard._farm_batch_submitted = 0
    dashboard._farm_batch_limit = policy.batch_size
    dashboard._farm_batch_resume_at = 0.0
    dashboard._farm_next_open_at = 0.0
    dashboard._farm_resource_pause_started = time.monotonic() - 330
    dashboard._farm_resource_pause_reason = "commit_critical"
    dashboard._farm_commit_wait_alerted = True
    dashboard._farm_commit_wait_last_log_at = 0.0
    dashboard._farm_commit_snapshot_percent = 91.2
    dashboard._farm_commit_snapshot_available_gib = 15.6
    dashboard._latest_profile_cpu_percent = 0.0
    dashboard.farm_launcher = FakeActionButton()
    dashboard._set_labeled_action_icon = lambda *_args, **_kwargs: None
    dashboard._cached_gpu_percent = lambda: None
    logs, events = [], []
    dashboard._append_log = logs.append
    dashboard._log_monitor_scheduler = lambda name, **payload: events.append(name)
    monkeypatch.setattr(
        dashboard_module, "get_windows_commit_snapshot",
        lambda: SimpleNamespace(critical=False),
    )
    monkeypatch.setattr(
        dashboard_module, "get_system_memory_status",
        lambda: SimpleNamespace(available_bytes=16 * 1024**3, load_percent=42.0),
    )

    dashboard._advance_farm_opening()

    assert dashboard.runner.commands == [("account-42", CommandKind.OPEN)]
    assert "farm_commit_recovered" in events
    assert dashboard._farm_commit_wait_alerted is False
    assert dashboard._farm_commit_snapshot_percent is None


def test_profile_launch_does_not_arrange_between_batches(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"account-{index}") for index in range(1, 4)]
    )
    # FakeFarmRunner deliberately has no arrange_windows method. This test
    # therefore also guards against restoring an intermediate arrangement.
    dashboard.runner = FakeFarmRunner(set())
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_launch_profiles = {f"account-{index}" for index in range(1, 4)}
    dashboard._farm_open_queue = deque(["account-3"])
    dashboard._farm_open_states = {
        "account-1": WorkerState.READY,
        "account-2": WorkerState.READY,
    }
    dashboard._farm_open_deadline = time.monotonic() + 60.0
    dashboard._farm_launch_policy = policy
    dashboard._farm_batch_profiles = {"account-1", "account-2"}
    dashboard._farm_batch_submitted = 2
    dashboard._farm_batch_limit = 2
    dashboard._farm_batch_resume_at = 0.0
    dashboard._farm_next_open_at = 0.0
    dashboard._farm_resource_pause_started = 0.0
    dashboard._farm_resource_pause_reason = None
    dashboard._latest_profile_cpu_percent = 0.0
    dashboard.farm_launcher = FakeActionButton()
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()
    monkeypatch.setattr(
        dashboard_module,
        "get_system_memory_status",
        lambda: SimpleNamespace(
            available_bytes=16 * 1_073_741_824,
            load_percent=40.0,
        ),
    )
    monkeypatch.setattr(dashboard_module, "get_gpu_utilization_percent", lambda: 20.0)
    monkeypatch.setattr(
        dashboard_module, "get_windows_commit_snapshot",
        lambda: SimpleNamespace(critical=False),
    )

    dashboard._advance_farm_opening()

    assert dashboard.runner.commands == [("account-3", CommandKind.OPEN)]
    assert dashboard._farm_batch_limit == policy.batch_size


def test_in_app_update_quits_before_deploy_and_restarts_release(monkeypatch) -> None:
    dashboard = Dashboard.__new__(Dashboard)
    logs: list[str] = []
    popen_calls: list[tuple[list[str], int]] = []
    quit_calls: list[bool] = []
    dashboard._append_log = logs.append

    monkeypatch.setattr(
        dashboard_module.QMessageBox,
        "information",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        dashboard_module.subprocess,
        "Popen",
        lambda args, creationflags=0: popen_calls.append((args, creationflags)),
    )
    fake_app = SimpleNamespace(quit=lambda: quit_calls.append(True))
    monkeypatch.setattr(
        dashboard_module,
        "QApplication",
        SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        dashboard_module,
        "QTimer",
        SimpleNamespace(singleShot=lambda _delay, callback: callback()),
    )

    dashboard._start_update_build(Path(r"D:\IKAutomation_Web"))

    assert len(popen_calls) == 1
    command = popen_calls[0][0][-1]
    assert "git pull --ff-only" in command
    assert "build-release.ps1 -NoDesktopShortcut -SkipProfileSync" in command
    assert command.index("build-release.ps1") < command.index("Start-Process")
    assert r"release\IK Auto\IK Auto.exe" in command
    assert quit_calls == [True]
    assert "nhả file executable" in logs[-1]