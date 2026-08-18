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


def test_dashboard_log_keeps_only_ten_latest_rows() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._log_lines = deque(maxlen=10)
    dashboard.log = FakeLogWidget()

    for index in range(15):
        dashboard._append_log(f"row-{index}")

    assert dashboard.log.value.splitlines() == [f"row-{index}" for index in range(5, 15)]
