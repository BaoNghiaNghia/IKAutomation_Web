from __future__ import annotations

import json

from ik_chrome_auto.chrome_preferences import suppress_browser_prompts


def test_suppress_browser_prompts_merges_without_removing_profile_state(tmp_path) -> None:
    preferences = tmp_path / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(
        json.dumps({"profile": {"name": "Account 01"}, "custom": {"keep": True}}),
        encoding="utf-8",
    )

    assert suppress_browser_prompts(tmp_path) == preferences
    payload = json.loads(preferences.read_text(encoding="utf-8"))

    assert payload["custom"] == {"keep": True}
    assert payload["profile"]["name"] == "Account 01"
    assert payload["credentials_enable_service"] is False
    assert payload["profile"]["password_manager_enabled"] is False
    assert payload["profile"]["default_content_setting_values"]["notifications"] == 2
    assert payload["profile"]["default_content_setting_values"]["geolocation"] == 2
    assert payload["autofill"]["profile_enabled"] is False
    assert payload["translate"]["enabled"] is False


def test_suppress_browser_prompts_creates_preferences_for_new_profile(tmp_path) -> None:
    preferences = suppress_browser_prompts(tmp_path)

    assert preferences.exists()
    payload = json.loads(preferences.read_text(encoding="utf-8"))
    assert payload["browser"]["has_seen_welcome_page"] is True
    assert payload["profile"]["exit_type"] == "Normal"
