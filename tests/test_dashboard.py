from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
import time

import ik_chrome_auto.dashboard as dashboard_module
from ik_chrome_auto.dashboard import (
    TAB_CLOSE_BATCH_PAUSE_SECONDS,
    TAB_CLOSE_INTERVAL_SECONDS,
    Dashboard,
    FarmProfileDialog,
    ProfileRow,
)
from ik_chrome_auto.farm_launch_policy import FarmLaunchPolicy
from ik_chrome_auto.models import CommandKind, WorkerSnapshot, WorkerState
from ik_chrome_auto.mail_monitor import (
    COMBAT_MAIL_OTHER,
    MAIL_BASELINE,
    NO_NEW_COMBAT_MAIL,
    TERRITORY_ATTACKED,
)


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

    def has_open_session(self, profile_id: str) -> bool:
        return profile_id in self.opened

    def submit(self, profile_id: str, command: CommandKind) -> None:
        self.commands.append((profile_id, command))

    def disable_sync(self) -> None:
        self.sync_enabled = False
        self.disable_sync_calls += 1

    def cancel_mail_monitor(self) -> None:
        self.mail_monitor_cancelled = True


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


def test_monitor_finishes_each_five_profile_group_before_starting_the_next() -> None:
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

    # The group is fixed at five. Its first pass is staggered so workers do
    # not execute the same UI step in lockstep.
    assert len(dashboard.runner.commands) == 1
    assert len(dashboard._monitor_in_flight) == 1
    assert len(dashboard._monitor_batch_pending) == 4
    assert len(dashboard._monitor_batch_profiles) == 5

    for _ in range(4):
        dashboard._monitor_next_profile_at = 0.0
        dashboard._advance_monitoring()

    assert len(dashboard.runner.commands) == 5
    assert len(dashboard._monitor_in_flight) == 5
    assert all(command == CommandKind.MONITOR_MAIL for _, command, _ in dashboard.runner.commands)
    initial_by_profile = {
        profile_id: bool(payload["initial_scan"])
        for profile_id, _command, payload in dashboard.runner.commands
    }
    assert initial_by_profile == {
        "account-0": True,
        "account-1": True,
        "account-2": True,
        "account-3": True,
        "account-4": True,
    }

    # A delayed profile must keep its slot in the group.  Previously the
    # 30-second advisory deadline removed it from the barrier and the next
    # group was queued while this worker was still running.
    dashboard._monitor_in_flight = {
        profile_id: 0.0 for profile_id in ("account-0", "account-1", "account-2", "account-3", "account-4")
    }
    dashboard._advance_monitoring()
    assert len(dashboard.runner.commands) == 5
    assert set(dashboard._monitor_in_flight) == {
        "account-0", "account-1", "account-2", "account-3", "account-4"
    }
    assert dashboard._monitor_batch_profiles == {
        "account-0", "account-1", "account-2", "account-3", "account-4"
    }

    # A completed member does not let the next profile leak into the current
    # group; all five baseline flows must finish first.
    dashboard._handle_monitor_result(
        WorkerSnapshot("account-0", monitor_events=(MAIL_BASELINE,))
    )
    dashboard._advance_monitoring()
    assert len(dashboard.runner.commands) == 5

    for profile_id in ("account-1", "account-2", "account-3", "account-4"):
        dashboard._handle_monitor_result(
            WorkerSnapshot(profile_id, monitor_events=(MAIL_BASELINE,))
        )
    assert dashboard._monitor_batch_phase == "combat"
    assert list(dashboard._monitor_batch_pending) == [
        "account-0", "account-1", "account-2", "account-3", "account-4"
    ]
    # The same members now complete pass 2 before account-5 is eligible.
    dashboard._monitor_next_profile_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1] == (
        "account-0", CommandKind.MONITOR_MAIL, {"initial_scan": False}
    )
    for _ in range(4):
        dashboard._monitor_next_profile_at = 0.0
        dashboard._advance_monitoring()
    assert [payload["initial_scan"] for _, _, payload in dashboard.runner.commands[-5:]] == [
        False,
        False,
        False,
        False,
        False,
    ]
    for profile_id in ("account-0", "account-1", "account-2", "account-3", "account-4"):
        dashboard._handle_monitor_result(
            WorkerSnapshot(profile_id, monitor_events=(NO_NEW_COMBAT_MAIL,))
        )
    dashboard._monitor_next_batch_at = 0.0
    dashboard._advance_monitoring()
    assert dashboard.runner.commands[-1][0] == "account-5"
    assert list(dashboard._monitor_batch_pending) == ["account-6"]


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
    assert "font-size:18px" in dashboard.sync_status_icon.style
    assert dashboard.sync_status_icon.tooltip == "Đồng bộ chuột - bàn phím đang tắt"

    dashboard._set_sync_status_indicator(True)
    assert "#16a34a" in dashboard.sync_status_icon.style
    assert dashboard.sync_status_icon.tooltip == "Đồng bộ chuột - bàn phím đang bật"


def test_sync_master_profile_id_is_stored_as_combo_user_data() -> None:
    combo = FakeProfileCombo()

    Dashboard._add_master_profile_option(combo, "cuongg********", "account-2")

    assert combo.items == [("cuongg********", None, "account-2")]


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


def test_farm_profile_picker_uses_two_columns_only_above_ten_profiles() -> None:
    assert FarmProfileDialog._column_count(1) == 1
    assert FarmProfileDialog._column_count(10) == 1
    assert FarmProfileDialog._column_count(11) == 2
    assert FarmProfileDialog._column_count(50) == 2


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
    dashboard.runner.sync_enabled = True

    dashboard._farm_all_action()

    assert dashboard.runner.sync_enabled is False
    assert dashboard.runner.disable_sync_calls == 1
    assert dashboard._farm_all_running is True
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


def test_profile_launch_sends_an_open_command_even_when_memory_is_constrained(monkeypatch) -> None:
    """A full RAM guard must throttle opening, not leave every selection queued."""
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
    dashboard.config = SimpleNamespace(profiles=[SimpleNamespace(id="account-1")])
    dashboard.runner = FakeFarmRunner(set())
    dashboard._farm_launcher_phase = "opening"
    dashboard._farm_launch_profiles = {"account-1"}
    dashboard._farm_open_queue = deque(["account-1"])
    dashboard._farm_open_states = {}
    dashboard._farm_open_deadline = time.monotonic() + 60.0
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

    dashboard._advance_farm_opening()

    assert dashboard.runner.commands == [("account-1", CommandKind.OPEN)]
    assert dashboard._farm_batch_limit == 1
    assert dashboard.farm_launcher.text == "Đang mở chậm"


def test_profile_launch_batch_stops_at_each_five_profile_layout_mark() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
    dashboard._farm_launch_profiles = {f"account-{index}" for index in range(1, 11)}
    dashboard._farm_open_states = {
        f"account-{index}": WorkerState.READY for index in range(1, 4)
    }
    dashboard._farm_last_arranged_ready_count = 0

    assert dashboard._next_farm_open_batch_limit(policy) == 2

    dashboard._farm_open_states.update(
        {f"account-{index}": WorkerState.READY for index in range(4, 9)}
    )
    dashboard._farm_last_arranged_ready_count = 5

    assert dashboard._next_farm_open_batch_limit(policy) == 2


def test_profile_launch_arranges_once_at_each_five_ready_profiles() -> None:
    class ArrangeRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[int, set[str]]] = []

        def arrange_windows(
            self,
            columns: int,
            *,
            profile_ids: set[str],
            layout_profile_ids: set[str],
        ) -> int:
            assert layout_profile_ids == dashboard._farm_launch_profiles
            self.calls.append((columns, set(profile_ids)))
            return len(profile_ids)

    dashboard = Dashboard.__new__(Dashboard)
    dashboard.runner = ArrangeRunner()
    dashboard._farm_launch_profiles = {f"account-{index}" for index in range(1, 11)}
    dashboard._farm_open_states = {
        f"account-{index}": WorkerState.READY for index in range(1, 6)
    }
    dashboard._farm_last_arranged_ready_count = 0
    dashboard._farm_arrange_columns = 4
    logs: list[str] = []
    dashboard._append_log = logs.append

    assert dashboard._arrange_farm_opening_milestone()
    assert dashboard._arrange_farm_opening_milestone()
    assert len(dashboard.runner.calls) == 1
    assert dashboard.runner.calls[0] == (4, {f"account-{index}" for index in range(1, 6)})

    dashboard._farm_open_states.update(
        {f"account-{index}": WorkerState.READY for index in range(6, 11)}
    )
    assert dashboard._arrange_farm_opening_milestone()

    assert len(dashboard.runner.calls) == 2
    assert dashboard._farm_last_arranged_ready_count == 10
    assert "Đã mở đủ 10 profile" in logs[-1]


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
