"""Prove the connection-health diagnostic is observation-only."""

from __future__ import annotations

from ik_chrome_auto import browser
from ik_chrome_auto.connection_health import collect_connection_health
from ik_chrome_auto.models import (
    AppConfig,
    BrowserSettings,
    CaptureSettings,
    ProfileConfig,
    ProfileMode,
)


def test_connection_health_inspects_forty_five_profiles_without_starting_chrome(
    tmp_path, monkeypatch
) -> None:
    profiles = [
        ProfileConfig(
            id=f"p-{index}",
            name=f"Profile {index}",
            user_data_dir=tmp_path / "profiles" / f"p-{index}",
            cdp_port=22000 + index,
        )
        for index in range(45)
    ]
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
        profiles=profiles,
    )
    inspected: list[str] = []

    class FakeProbe:
        def __init__(self, _config, profile) -> None:
            self.profile = profile
            inspected.append(profile.id)

        def _managed_attach_candidates(self, **_kwargs):
            return ((self.profile.cdp_port, "configured"),)

        def _select_managed_cdp_endpoint(self, _candidates):
            index = int(self.profile.id.split("-")[1])
            if index < 43:
                return f"http://127.0.0.1:{self.profile.cdp_port}", [], "configured"
            return None, [{"reason": "endpoint_not_ready"}], None

        def _matching_profile_chrome_processes(self):
            index = int(self.profile.id.split("-")[1])
            if index == 43:
                return [4321], [4321], "ok"
            return [], [], "ok"

    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})
    monkeypatch.setattr(browser, "ChromeProfileSession", FakeProbe)
    report = collect_connection_health(config)

    assert len(inspected) == 45
    assert report["configured_enabled"] == 45
    assert report["verified_cdp"] == 43
    assert report["states"] == {
        "chrome_not_detected": 1,
        "chrome_without_verified_cdp": 1,
        "verified_cdp": 43,
    }
    assert len(report["profiles"]) == 45
    assert all("user_data_dir" not in item for item in report["profiles"])


def test_disabled_profile_is_not_probed(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        root=tmp_path,
        source=tmp_path / "config.json",
        target_url="https://ik.playfun.vn/login-game",
        data_dir=tmp_path / "data",
        browser=BrowserSettings(),
        capture=CaptureSettings(),
        profiles=[
            ProfileConfig("disabled", "Disabled", enabled=False),
            ProfileConfig("external", "External", mode=ProfileMode.CDP,
                          cdp_url="http://127.0.0.1:22222"),
        ],
    )
    monkeypatch.setattr(browser, "_live_managed_cdp_ports", lambda: {})
    monkeypatch.setattr(
        browser, "ChromeProfileSession",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("No browser session should start for external CDP")
        ),
    )
    report = collect_connection_health(config)
    assert report["configured_enabled"] == 1
    assert report["profiles"][0]["profile_id"] == "external"
    assert report["profiles"][0]["state"] == "external_cdp_not_verified"