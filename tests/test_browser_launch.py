from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_live_managed_cdp_ports_reads_the_running_chrome_command_line(monkeypatch) -> None:
    browser._CDP_PROCESS_DISCOVERY_CACHE = (0.0, {})
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="  TCP    127.0.0.1:9222     0.0.0.0:0     LISTENING     777\n"
        ),
    )
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda: (
            {
                777: 'chrome --user-data-dir="D:/IK/profiles/account-1" '
                "--remote-debugging-port=9222"
            },
            "ok",
        ),
    )

    assert browser._live_managed_cdp_ports() == {"d:\\ik\\profiles\\account-1": 9222}


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


def test_known_direct_play_session_error_is_detected_only_for_play_route() -> None:
    session = browser.ChromeProfileSession.__new__(browser.ChromeProfileSession)
    session._page = type(
        "Page",
        (), {
            "locator": lambda _self, _selector: type(
                "Body", (), {"inner_text": lambda _self, **_kwargs: '{\"code\":141303,\"msg\":211155,\"data\":[]}'},
            )()
        },
    )()

    assert session._is_portal_direct_play_session_error("https://ik.playfun.vn/play-game") is True
    assert session._is_portal_direct_play_session_error("https://ik.playfun.vn/login-game") is False


def test_managed_attach_rejects_a_known_foreign_user_data_dir(tmp_path: Path, monkeypatch) -> None:
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
    )
    session = browser.ChromeProfileSession(
        config,
        ProfileConfig("account-a", "Account A", user_data_dir=tmp_path / "data" / "profiles" / "a", cdp_port=21101),
    )
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda _endpoint: True)
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 777)
    monkeypatch.setattr(browser, "get_process_command_line", lambda _pid: f"chrome --user-data-dir={tmp_path / 'data' / 'profiles' / 'b'}")

    assert session.can_attach_existing_browser() is False


def test_managed_attach_accepts_equivalent_windows_user_data_dir_spelling(
    tmp_path: Path, monkeypatch
) -> None:
    profile_dir = tmp_path / "data" / "profiles" / "a"
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
    )
    session = browser.ChromeProfileSession(
        config, ProfileConfig("account-a", "Account A", user_data_dir=profile_dir, cdp_port=21101)
    )
    spelling = str(profile_dir).replace("\\", "/") + "/"
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda _endpoint: True)
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 777)
    monkeypatch.setattr(
        browser, "get_process_command_line", lambda _pid: f'chrome --user-data-dir="{spelling}"'
    )

    assert session.can_attach_existing_browser() is True


def test_managed_attach_uses_live_devtools_active_port_after_configuration_change(
    tmp_path: Path, monkeypatch
) -> None:
    profile_dir = tmp_path / "data" / "profiles" / "retained"
    profile_dir.mkdir(parents=True)
    (profile_dir / "DevToolsActivePort").write_text("27654\n/devtools/browser/test\n")
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
    )
    session = browser.ChromeProfileSession(
        config,
        ProfileConfig("retained", "Retained", user_data_dir=profile_dir, cdp_port=21101),
    )
    attempted: list[str] = []
    monkeypatch.setattr(
        browser,
        "_cdp_endpoint_is_ready",
        lambda endpoint: (attempted.append(endpoint) or endpoint.endswith(":27654")),
    )
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 777)
    monkeypatch.setattr(
        browser,
        "get_process_command_line",
        lambda _pid: f'chrome --user-data-dir="{profile_dir}"',
    )

    assert session.can_attach_existing_browser() is True
    assert attempted[0] == "http://127.0.0.1:27654"


def test_choose_page_prefers_game_portal_over_unrelated_tabs() -> None:
    class Page:
        def __init__(self, url: str) -> None:
            self.url = url

        def is_closed(self) -> bool:
            return False

    session = browser.ChromeProfileSession.__new__(browser.ChromeProfileSession)
    session.config = AppConfig(
        root=Path("."), source=Path("config.json"), target_url="https://ik.playfun.vn/login-game",
        data_dir=Path("data"), browser=BrowserSettings(), capture=CaptureSettings(),
    )
    game = Page("https://ik.playfun.vn/play-game")
    session._context = type("Context", (), {"pages": [Page("about:blank"), Page("https://example.com"), game]})()

    assert session._choose_page() is game
