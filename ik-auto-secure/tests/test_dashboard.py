from __future__ import annotations

from collections import deque

from ik_chrome_auto.dashboard import Dashboard


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
