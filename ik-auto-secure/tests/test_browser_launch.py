from __future__ import annotations

from pathlib import Path

from ik_chrome_auto import browser
from ik_chrome_auto.models import AppConfig, BrowserSettings, CaptureSettings, ProfileConfig


class _FakeChromium:
    def __init__(self) -> None:
        self.options: dict[str, object] | None = None

    def launch_persistent_context(self, **options: object) -> object:
        self.options = options
        return object()


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()


def test_managed_chrome_launch_keeps_chromium_sandbox_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/play-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(headless=True),
        capture=CaptureSettings(),
    )
    profile = ProfileConfig(
        id="main",
        name="Main",
        user_data_dir=tmp_path / "data" / "profiles" / "main",
    )
    session = browser.ChromeProfileSession(config, profile)
    fake_playwright = _FakePlaywright()
    session._playwright = fake_playwright  # type: ignore[assignment]
    monkeypatch.setattr(browser, "find_chrome", lambda _configured: Path("chrome.exe"))

    session._start_managed()

    assert fake_playwright.chromium.options is not None
    assert fake_playwright.chromium.options["chromium_sandbox"] is True
