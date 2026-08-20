from __future__ import annotations

from collections import deque

from ik_chrome_auto.dashboard import Dashboard, FarmProfileDialog


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


def test_dashboard_log_keeps_only_ten_latest_rows() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    for index in range(15):
        dashboard._append_log(f"row-{index}")

    assert dashboard.log.value.splitlines() == [f"row-{index}" for index in range(5, 15)]


def test_mouse_sync_section_is_collapsed_by_default_and_can_toggle() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync_section_expanded = False
    dashboard.sync_section = FakeCollapsibleWidget()
    dashboard.sync_section_toggle = FakeToggleButton()

    dashboard._toggle_sync_section()
    assert dashboard.sync_section_expanded is True
    assert dashboard.sync_section.visible is True
    assert dashboard.sync_section_toggle.tooltip == "Thu gọn Đồng bộ chuột"

    dashboard._toggle_sync_section()
    assert dashboard.sync_section_expanded is False
    assert dashboard.sync_section.visible is False
    assert dashboard.sync_section_toggle.tooltip == "Mở rộng Đồng bộ chuột"


def test_mouse_sync_status_indicator_changes_colour_and_tooltip() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.sync_status_icon = FakeStatusIcon()

    dashboard._set_sync_status_indicator(False)
    assert "#94a3b8" in dashboard.sync_status_icon.style
    assert "font-size:22px" in dashboard.sync_status_icon.style
    assert dashboard.sync_status_icon.tooltip == "Đồng bộ chuột đang tắt"

    dashboard._set_sync_status_indicator(True)
    assert "#16a34a" in dashboard.sync_status_icon.style
    assert dashboard.sync_status_icon.tooltip == "Đồng bộ chuột đang bật"


def test_sync_master_profile_id_is_stored_as_combo_user_data() -> None:
    combo = FakeProfileCombo()

    Dashboard._add_master_profile_option(combo, "cuongg********", "account-2")

    assert combo.items == [("cuongg********", None, "account-2")]


def test_dashboard_uses_more_space_on_compact_screens_and_half_on_desktop() -> None:
    compact = Dashboard._responsive_metrics(1366, 768)
    desktop = Dashboard._responsive_metrics(1920, 1080)

    assert compact == (984, 553, 305, 354, True)
    assert desktop == (960, 540, 298, 346, False)


def test_dashboard_never_exceeds_small_logical_screen_geometry() -> None:
    width, height, sidebar_min, sidebar_max, compact = Dashboard._responsive_metrics(640, 400)

    assert (width, height) == (624, 384)
    assert sidebar_min <= sidebar_max < width
    assert compact is True


def test_dashboard_uses_smaller_typography_on_high_dpi_logical_screen() -> None:
    dense = Dashboard._responsive_typography(960, 540)
    compact = Dashboard._responsive_typography(1366, 768)
    desktop = Dashboard._responsive_typography(1920, 1080)

    assert dense == (7.25, 7.0, 12.0, 6.5, 9.25, 30)
    assert dense[0] < compact[0] < desktop[0]
    assert dense[-1] < compact[-1] < desktop[-1]


def test_farm_profile_picker_uses_two_columns_only_above_ten_profiles() -> None:
    assert FarmProfileDialog._column_count(1) == 1
    assert FarmProfileDialog._column_count(10) == 1
    assert FarmProfileDialog._column_count(11) == 2
    assert FarmProfileDialog._column_count(50) == 2
