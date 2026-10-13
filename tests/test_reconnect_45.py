"""Regression gates for restart and reconnect with 45 retained Chrome profiles."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ik_chrome_auto import browser
from ik_chrome_auto.dashboard import Dashboard
from ik_chrome_auto.models import (
    AppConfig,
    BrowserSettings,
    CaptureSettings,
    CommandKind,
    ProfileConfig,
    WorkerSnapshot,
    WorkerState,
)


class Button:
    def __init__(self) -> None:
        self.enabled = True
        self.text = ""

    def setEnabled(self, value: bool) -> None:  # noqa: N802
        self.enabled = value

    def setText(self, value: str) -> None:  # noqa: N802
        self.text = value


def test_forty_five_profiles_reattach_with_four_slots_and_never_repair() -> None:
    profile_ids = [f"account-{i:02d}" for i in range(45)]
    calls: list[tuple[str, CommandKind]] = []
    d = Dashboard.__new__(Dashboard)
    d.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=i, enabled=True) for i in profile_ids],
        browser=SimpleNamespace(startup_timeout_ms=90_000),
    )
    d.runner = SimpleNamespace(
        is_attached=lambda _id: False,
        has_open_session=lambda _id: True,
        submit=lambda i, command: calls.append((i, command)),
    )
    d.farm_launcher = Button()
    d._farm_launcher_phase = "launch"
    logs: list[str] = []
    d._append_log = logs.append
    refreshed: list[bool] = []
    d._refresh_sync_control = lambda: refreshed.append(True)

    d._begin_profile_reattach()
    assert len(calls) == 4
    assert len(d._reattach_in_flight) == 4
    assert d.farm_launcher.enabled is False
    while d._reattach_pending_profiles:
        active = tuple(d._reattach_in_flight)
        assert active
        for pid in active:
            d._settle_profile_reattach(WorkerSnapshot(pid, WorkerState.READY, "CDP attached"))
        assert len(d._reattach_in_flight) <= 4

    assert len(calls) == 45
    assert set(i for i, _command in calls) == set(profile_ids)
    assert all(command is CommandKind.ATTACH for _id, command in calls)
    assert d._reattach_failed_profiles == set()
    assert d.farm_launcher.enabled is True
    assert refreshed == [True]


def test_one_reattach_timeout_frees_slot_without_killing_chrome() -> None:
    profiles = [f"account-{i}" for i in range(6)]
    calls: list[tuple[str, CommandKind]] = []
    d = Dashboard.__new__(Dashboard)
    d.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=i, enabled=True) for i in profiles],
        browser=SimpleNamespace(startup_timeout_ms=90_000),
    )
    d.runner = SimpleNamespace(
        is_attached=lambda _id: False,
        has_open_session=lambda _id: False,
        submit=lambda i, cmd: calls.append((i, cmd)),
    )
    d.farm_launcher = Button()
    d._farm_launcher_phase = "launch"
    d._append_log = lambda _: None
    d._refresh_sync_control = lambda: None
    d._begin_profile_reattach()
    timed_out = calls[0][0]
    d._reattach_deadlines[timed_out] = 0.0

    d._expire_profile_reattach()

    assert timed_out in d._reattach_failed_profiles
    assert timed_out not in d._reattach_pending_profiles
    assert len(calls) == 5
    assert all(cmd is CommandKind.ATTACH for _, cmd in calls)


def test_uncertain_cdp_port_cannot_attach_to_wrong_profile(tmp_path, monkeypatch) -> None:
    user_data_dir = tmp_path / "chrome" / "profile-a"
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
        ProfileConfig("profile-a", "Profile A", user_data_dir=user_data_dir, cdp_port=21047),
    )
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda _url: True)
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 56789)
    monkeypatch.setattr(browser, "_chrome_process_command_lines", lambda **_: ({}, "access_denied"))
    monkeypatch.setattr(browser, "snapshot_process_parents", lambda: {})

    assert session.can_attach_existing_browser() is False
    assert session.attach_diagnostics()["matched_endpoint"] is None
    assert all(
        row["identity"] != "match"
        for row in session.attach_diagnostics()["candidate_results"]
    )


def test_cdp_listener_can_be_verified_through_chrome_parent(tmp_path, monkeypatch) -> None:
    user_data_dir = tmp_path / "chrome" / "profile-a"
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
        ProfileConfig("profile-a", "Profile A", user_data_dir=user_data_dir, cdp_port=21048),
    )
    monkeypatch.setattr(browser, "_cdp_endpoint_is_ready", lambda _url: True)
    monkeypatch.setattr(browser, "find_tcp_listener_process", lambda _port: 456)
    monkeypatch.setattr(
        browser,
        "_chrome_process_command_lines",
        lambda **_: ({123: f'chrome --user-data-dir="{user_data_dir}"'}, "ok"),
    )
    monkeypatch.setattr(browser, "snapshot_process_parents", lambda: {456: 123})

    assert session.can_attach_existing_browser() is True
    assert session.attach_diagnostics()["matched_endpoint"] == "http://127.0.0.1:21048"
    assert session._managed_browser_pid == 123  # Verified ancestor owns the Chrome window


def test_repair_is_never_allowed_to_terminate_chrome_implicitly() -> None:
    session = browser.ChromeProfileSession.__new__(browser.ChromeProfileSession)
    session._last_attach_probe = {}

    assert session.repair_existing_browser_without_cdp() is False
    assert session.attach_diagnostics()["repair_result"] == "restart_not_authorized"

def test_open_refuses_duplicate_managed_chrome_if_old_session_has_no_cdp(
    tmp_path, monkeypatch
) -> None:
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
        ProfileConfig(
            "account-1", "Account 1",
            user_data_dir=tmp_path / "data" / "profiles" / "account-1",
        ),
    )
    session._playwright = object()
    monkeypatch.setattr(session, "_discover_managed_cdp_endpoint", lambda: None)
    monkeypatch.setattr(session, "_matching_profile_chrome_processes", lambda: ([1234], [1234], "ok"))
    monkeypatch.setattr(
        browser.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn Chrome")),
    )

    with pytest.raises(RuntimeError, match="đang mở"):
        session._start_managed()


def test_retry_only_targets_failed_and_unattached_profile() -> None:
    calls: list[tuple[str, CommandKind]] = []
    ready = {"account-1", "account-2"}
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[
            SimpleNamespace(id=name, enabled=True)
            for name in ("account-1", "account-2", "account-3")
        ],
        browser=SimpleNamespace(startup_timeout_ms=90_000),
    )
    dashboard.runner = SimpleNamespace(
        is_attached=lambda pid: pid in ready,
        has_open_session=lambda pid: pid in ready,
        submit=lambda pid, cmd: calls.append((pid, cmd)),
    )
    dashboard._farm_launcher_phase = "launch"
    dashboard.farm_launcher = Button()
    dashboard._reattach_pending_profiles = set()
    dashboard._reattach_failed_profiles = {"account-2", "account-3"}
    dashboard._append_log = lambda _: None
    dashboard._refresh_sync_control = lambda: None

    dashboard._retry_profile_reattach()

    assert calls == [("account-3", CommandKind.ATTACH)]
    assert dashboard._reattach_pending_profiles == {"account-3"}


def test_late_ready_event_clears_false_failed_profile() -> None:
    ready: set[str] = set()
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id="account-1", enabled=True)],
        browser=SimpleNamespace(startup_timeout_ms=30_000),
    )
    dashboard.runner = SimpleNamespace(
        is_attached=lambda pid: pid in ready,
        has_open_session=lambda pid: pid in ready,
        submit=lambda *_args: None,
    )
    dashboard.farm_launcher = Button()
    dashboard._farm_launcher_phase = "launch"
    dashboard._append_log = lambda _: None
    dashboard._refresh_sync_control = lambda: None
    dashboard._begin_profile_reattach()
    dashboard._reattach_deadlines["account-1"] = 0
    dashboard._expire_profile_reattach()
    assert "account-1" in dashboard._reattach_failed_profiles

    ready.add("account-1")
    dashboard._settle_profile_reattach(
        WorkerSnapshot("account-1", WorkerState.READY, "late attached")
    )

    assert dashboard._reattach_failed_profiles == set()
    assert dashboard._reattach_pending_profiles == set()


def test_reconnect_health_counts_live_attached_not_stale_job_counters() -> None:
    class TextHealth:
        value = ""

        def setText(self, value: str) -> None:  # noqa: N802
            self.value = value

        def setToolTip(self, _: str) -> None:  # noqa: N802
            pass

    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"p-{i}", enabled=True) for i in range(45)]
    )
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda pid: int(pid.split("-")[1]) < 43
    )
    dashboard.reconnect_health = TextHealth()
    dashboard._reattach_total = 1
    dashboard._reattach_pending_profiles = set()
    dashboard._reattach_failed_profiles = {"p-44"}

    dashboard._update_reconnect_health()

    assert dashboard.reconnect_health.value == "CDP 43/45"


def test_45_capacity_health_shows_missing_unique_accounts() -> None:
    class Status:
        value = ""
        tooltip = ""
        def setText(self, value: str) -> None:
            self.value = value
        def setToolTip(self, value: str) -> None:
            self.tooltip = value

    dashboard = Dashboard.__new__(Dashboard)
    dashboard.config = SimpleNamespace(
        profiles=[SimpleNamespace(id=f"p-{i}", enabled=True) for i in range(43)]
    )
    dashboard.runner = SimpleNamespace(
        has_open_session=lambda pid: int(pid.split("-")[1]) < 36
    )
    dashboard.reconnect_health = Status()
    dashboard._reattach_pending_profiles = set()

    dashboard._update_reconnect_health()

    assert dashboard.reconnect_health.value == "CDP 36/43 · mục tiêu 45"
    assert "2 tài khoản duy nhất" in dashboard.reconnect_health.tooltip
    assert "90%" in dashboard.reconnect_health.tooltip


def test_failed_windows_process_discovery_never_launches_duplicate_chrome(
    tmp_path, monkeypatch
) -> None:
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
        ProfileConfig(
            "account-1", "Account 1",
            user_data_dir=tmp_path / "data" / "profiles" / "account-1",
        ),
    )
    session._playwright = object()
    monkeypatch.setattr(session, "_discover_managed_cdp_endpoint", lambda: None)
    monkeypatch.setattr(
        session, "_matching_profile_chrome_processes",
        lambda: ([], [], "query_failed"),
    )
    monkeypatch.setattr(
        browser.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not start Chrome without process evidence")
        ),
    )

    with pytest.raises(RuntimeError, match="có thể đang mở"):
        session._start_managed()
