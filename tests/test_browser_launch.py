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


def test_repair_restarts_only_the_exact_profile_user_data_dir(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "data" / "profiles" / "account-1"
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
        ProfileConfig("account-1", "Account 1", user_data_dir=profile_dir),
    )
    other_dir = tmp_path / "other"
    commands = {
        111: f'chrome --user-data-dir="{profile_dir}"',
        222: f'chrome --user-data-dir="{other_dir}"',
    }
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_kwargs: (commands, "ok"),
    )
    monkeypatch.setattr(browser, "snapshot_process_parents", lambda: {111: 1, 222: 1})
    calls: list[list[str]] = []
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda args, **_kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )

    assert session.repair_existing_browser_without_cdp() is True
    assert calls == [["taskkill", "/PID", "111", "/T", "/F"]]
    assert session.attach_diagnostics()["repair_result"] == "terminated"


def test_repair_never_terminates_when_profile_process_is_not_unique(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "data" / "profiles" / "account-1"
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
        ProfileConfig("account-1", "Account 1", user_data_dir=profile_dir),
    )
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_kwargs: (
            {
                111: f'chrome --user-data-dir="{profile_dir}"',
                222: f'chrome --user-data-dir="{profile_dir}"',
            },
            "ok",
        ),
    )
    monkeypatch.setattr(browser, "snapshot_process_parents", lambda: {111: 1, 222: 1})
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not taskkill")),
    )

    assert session.repair_existing_browser_without_cdp() is False
    assert session.attach_diagnostics()["repair_result"] == "no_unique_profile_root"


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


def test_retained_attach_reuses_the_verified_endpoint_without_rediscovery(
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
    session = browser.ChromeProfileSession(
        config,
        ProfileConfig("main", "Main", user_data_dir=tmp_path / "data" / "profiles" / "main"),
    )
    fake_playwright = _FakePlaywright()
    session._playwright = fake_playwright  # type: ignore[assignment]
    session._managed_cdp_endpoint = "http://127.0.0.1:21901"
    monkeypatch.setattr(
        browser,
        "find_chrome",
        lambda _configured: (_ for _ in ()).throw(AssertionError("must not rediscover")),
    )

    session._start_managed()

    assert fake_playwright.chromium.endpoint == "http://127.0.0.1:21901"


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
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})
    monkeypatch.setattr(
        browser, "_live_managed_cdp_port_diagnostics", lambda: {"status": "query_failed"}
    )
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 777)
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_kwargs: (
            {777: f"chrome --user-data-dir={tmp_path / 'data' / 'profiles' / 'b'}"},
            "ok",
        ),
    )

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
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})
    monkeypatch.setattr(
        browser, "_live_managed_cdp_port_diagnostics", lambda: {"status": "query_failed"}
    )
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 777)
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_kwargs: ({777: f'chrome --user-data-dir="{spelling}"'}, "ok"),
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
        "_chrome_process_command_lines",
        lambda **_kwargs: ({777: f'chrome --user-data-dir="{profile_dir}"'}, "ok"),
    )

    assert session.can_attach_existing_browser() is True
    assert attempted[:2] == ["http://127.0.0.1:21101", "http://127.0.0.1:27654"]


def test_configured_port_survives_successful_but_incomplete_global_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    profile_dir = tmp_path / "data" / "profiles" / "account-3"
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
        ProfileConfig("account-3", "Account 3", user_data_dir=profile_dir, cdp_port=21901),
    )
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda endpoint: endpoint.endswith(":21901"))
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 777)
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_kwargs: ({777: f'chrome --user-data-dir="{profile_dir}"'}, "ok"),
    )
    monkeypatch.setattr(
        browser,
        "_live_managed_cdp_port_diagnostics",
        lambda: {
            "status": "ok",
            "chrome_process_count": 447,
            "managed_cdp_process_count": 1,
        },
    )
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})

    assert session.can_attach_existing_browser() is True
    diagnostics = session.attach_diagnostics()
    assert diagnostics["candidate_ports"][0] == 21901
    assert diagnostics["matched_endpoint"] == "http://127.0.0.1:21901"
    assert diagnostics["candidate_results"][0]["identity"] == "match"


def test_foreign_configured_port_is_rejected_before_trying_legacy_port(
    tmp_path: Path, monkeypatch
) -> None:
    profile_dir = tmp_path / "data" / "profiles" / "account-3"
    foreign_dir = tmp_path / "data" / "profiles" / "account-4"
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
        ProfileConfig("account-3", "Account 3", user_data_dir=profile_dir, cdp_port=21901),
    )
    monkeypatch.setattr(browser, "_profile_cdp_port", lambda _profile_id: 22901)
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda _endpoint: True)
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda port: 11 if port == 21901 else 22)
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})
    monkeypatch.setattr(browser, "_live_managed_cdp_port_diagnostics", lambda: {"status": "ok"})
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_kwargs: (
            {
                11: f'chrome --user-data-dir="{foreign_dir}"',
                22: f'chrome --user-data-dir="{profile_dir}"',
            },
            "ok",
        ),
    )

    assert session.can_attach_existing_browser() is True
    diagnostics = session.attach_diagnostics()
    assert diagnostics["matched_endpoint"] == "http://127.0.0.1:22901"
    assert diagnostics["candidate_results"][0]["identity"] == "foreign"


def test_45_healthy_configured_profiles_attach_without_global_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    ports = {f"account-{index}": 22000 + index for index in range(45)}
    profile_dirs = {
        profile_id: tmp_path / "data" / "profiles" / profile_id for profile_id in ports
    }
    pid_by_port = {port: 1000 + index for index, port in enumerate(ports.values())}
    commands = {
        pid_by_port[port]: f'chrome --user-data-dir="{profile_dirs[profile_id]}"'
        for profile_id, port in ports.items()
    }
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
    )
    global_calls = 0

    def global_ports() -> dict[str, int]:
        nonlocal global_calls
        global_calls += 1
        return {next(iter(profile_dirs.values())).as_posix(): next(iter(ports.values()))}

    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda endpoint: int(endpoint.rsplit(":", 1)[-1]) in pid_by_port)
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda port: pid_by_port[port])
    monkeypatch.setattr(browser, "_chrome_process_command_lines", lambda **_kwargs: (commands, "ok"))
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", global_ports)
    monkeypatch.setattr(browser, "_live_managed_cdp_port_diagnostics", lambda: {"status": "ok", "managed_cdp_process_count": 1})
    monkeypatch.setattr(browser, "find_chrome_window", lambda _title: None)
    monkeypatch.setattr(browser, "find_chrome_window_for_process", lambda _pid: None)

    sessions = [
        browser.ChromeProfileSession(
            config,
            ProfileConfig(profile_id, profile_id, user_data_dir=profile_dirs[profile_id], cdp_port=port),
        )
        for profile_id, port in ports.items()
    ]

    assert all(session.can_attach_existing_browser() for session in sessions)
    assert all(session.attach_diagnostics()["matched_endpoint"] is not None for session in sessions)
    assert global_calls == 0


def test_45_profile_reconnect_one_dead_port_does_not_block_44_healthy_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    ports = {f"account-{index}": 23000 + index for index in range(45)}
    dead_profile = "account-44"
    profile_dirs = {
        profile_id: tmp_path / "data" / "profiles" / profile_id for profile_id in ports
    }
    pid_by_port = {port: 2000 + index for index, port in enumerate(ports.values())}
    commands = {
        pid_by_port[port]: f'chrome --user-data-dir="{profile_dirs[profile_id]}"'
        for profile_id, port in ports.items()
    }
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
    )
    dead_port = ports[dead_profile]
    monkeypatch.setattr(
        browser,
        "_cdp_endpoint_is_ready",
        lambda endpoint: int(endpoint.rsplit(":", 1)[-1]) in pid_by_port
        and int(endpoint.rsplit(":", 1)[-1]) != dead_port,
    )
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda port: pid_by_port[port])
    monkeypatch.setattr(browser, "_chrome_process_command_lines", lambda **_kwargs: (commands, "ok"))
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})
    monkeypatch.setattr(browser, "_live_managed_cdp_port_diagnostics", lambda: {"status": "ok", "managed_cdp_process_count": 1})
    monkeypatch.setattr(browser, "find_chrome_window", lambda _title: None)
    monkeypatch.setattr(browser, "find_chrome_window_for_process", lambda _pid: None)
    sessions = {
        profile_id: browser.ChromeProfileSession(
            config,
            ProfileConfig(profile_id, profile_id, user_data_dir=profile_dirs[profile_id], cdp_port=port),
        )
        for profile_id, port in ports.items()
    }

    attached = [profile_id for profile_id, session in sessions.items() if session.can_attach_existing_browser()]
    assert len(attached) == 44
    assert dead_profile not in attached
    assert sessions[dead_profile].attach_diagnostics()["candidate_ports"][0] == dead_port


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
