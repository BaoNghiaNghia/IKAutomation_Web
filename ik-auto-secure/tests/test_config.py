from __future__ import annotations

import json
from pathlib import Path

import pytest

from ik_chrome_auto.config import is_allowed_url, load_config, save_config, unique_profile_id
from ik_chrome_auto.models import Auto2048Speed, ProfileMode


def test_load_resolves_relative_paths(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "target_url": "https://ik.playfun.vn/play-game",
                "data_dir": "data",
                "profiles": [
                    {
                        "id": "Main Account",
                        "name": "Main",
                        "mode": "managed",
                        "user_data_dir": "data/profiles/main",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(source)

    assert config.data_dir == (tmp_path / "data").resolve()
    assert config.profiles[0].id == "main-account"
    assert config.profiles[0].mode == ProfileMode.MANAGED
    assert config.profiles[0].user_data_dir == (tmp_path / "data/profiles/main").resolve()
    assert config.browser.auto_resize is True
    assert config.browser.app_mode is True
    assert config.browser.profile_title is True
    assert config.browser.low_memory_mode is True
    assert config.browser.viewport_width == 500
    assert config.browser.viewport_height == 281
    assert config.browser.windows_per_row == 6
    assert config.auto_2048_speed == Auto2048Speed.BALANCED
    assert config.capture.capture_response_bodies is False
    assert config.capture.network_capture_enabled is False


def test_allowed_url_checks_real_hostname_boundaries() -> None:
    hosts = ("ik.playfun.vn", "gtarcade.com")

    assert is_allowed_url("https://ik.playfun.vn/play-game", hosts)
    assert is_allowed_url("https://assets.gtarcade.com/game", hosts)
    assert not is_allowed_url("https://evil.example/?next=ik.playfun.vn", hosts)
    assert not is_allowed_url("https://notgtarcade.com/game", hosts)
    assert not is_allowed_url("file:///C:/secret.txt", hosts)


def test_rejects_target_outside_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps({"target_url": "https://evil.example/game", "profiles": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="target_url"):
        load_config(source)


def test_save_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "data_dir": "data",
                "profiles": [
                    {"id": "main", "name": "Main", "mode": "managed"}
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(source)
    config.auto_2048_speed = Auto2048Speed.FAST
    config.browser.low_memory_mode = False
    config.browser.windows_per_row = 6

    save_config(config)
    loaded = load_config(source)

    assert loaded.target_url == "https://ik.playfun.vn/play-game"
    assert loaded.profiles[0].user_data_dir == (tmp_path / "data/profiles/main").resolve()
    assert loaded.browser.auto_resize is True
    assert loaded.browser.app_mode is True
    assert loaded.browser.low_memory_mode is False
    assert loaded.browser.windows_per_row == 6
    assert loaded.auto_2048_speed == Auto2048Speed.FAST


def test_unique_profile_id() -> None:
    assert unique_profile_id("Farm 01", set()) == "farm-01"
    assert unique_profile_id("Farm 01", {"farm-01", "farm-01-2"}) == "farm-01-3"
