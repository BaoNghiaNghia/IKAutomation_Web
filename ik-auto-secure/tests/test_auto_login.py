from __future__ import annotations

import sys
import types
from pathlib import Path

from ik_chrome_auto import browser
from ik_chrome_auto.models import AppConfig, BrowserSettings, CaptureSettings, ProfileConfig


class _Input:
    def __init__(self) -> None:
        self.value = ""

    def fill(self, value: str, *, timeout: int) -> None:
        self.value = value


class _Button:
    class _First:
        def __init__(self, parent: "_Button") -> None:
            self.parent = parent

        def click(self, *, timeout: int) -> None:
            self.parent.clicked = True

    def __init__(self) -> None:
        self.clicked = False
        self.first = self._First(self)


class _Frame:
    url = "https://ik.playfun.vn/play-game"

    def __init__(self, button: _Button) -> None:
        self.button = button

    def get_by_role(self, _role: str, *, name: object) -> _Button:
        return self.button


class _Page:
    def __init__(self, frame: _Frame) -> None:
        self.frames = [frame]

    def is_closed(self) -> bool:
        return False


def test_auto_login_fills_form_and_clicks_login(tmp_path: Path, monkeypatch) -> None:
    config = AppConfig(
        root=tmp_path, source=tmp_path / "config.json", target_url="https://ik.playfun.vn/play-game",
        data_dir=tmp_path / "data", browser=BrowserSettings(headless=True), capture=CaptureSettings(),
    )
    session = browser.ChromeProfileSession(config, ProfileConfig(id="account-1", name="Account"))
    button, username, password = _Button(), _Input(), _Input()
    frame = _Frame(button)
    session._page = _Page(frame)  # type: ignore[assignment]
    values = iter([username, password])
    monkeypatch.setattr(session, "_first_visible_input", lambda *_args: next(values))

    class Store:
        def load(self, _profile_id: str) -> object:
            return types.SimpleNamespace(username="user@example.com", password="secret")

    monkeypatch.setitem(
        sys.modules,
        "ik_chrome_auto.credential_store",
        types.SimpleNamespace(WindowsCredentialStore=Store),
    )

    assert session.auto_login_if_needed() is True
    assert username.value == "user@example.com"
    assert password.value == "secret"
    assert button.clicked is True
