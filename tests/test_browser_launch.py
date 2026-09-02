from __future__ import annotations

from pathlib import Path

from ik_chrome_auto import browser
from ik_chrome_auto.models import AppConfig, BrowserSettings, CaptureSettings, ProfileConfig


class _FakeBrowser:
    contexts = [object()]


class _FakeChromium:
    def __init__(self) -> None:
        self.endpoint: str | None = None

    def connect_over_cdp(self, endpoint: str, **_options: object) -> _FakeBrowser:
        self.endpoint = endpoint
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()


def test_managed_chrome_launches_detached_and_connects_over_stable_cdp(
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
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda _endpoint: True)

    session._start_managed()

    expected = f"http://127.0.0.1:{browser._profile_cdp_port('main')}"
    assert fake_playwright.chromium.endpoint == expected
