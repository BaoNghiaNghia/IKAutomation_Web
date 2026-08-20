"""Managed Chrome preferences that suppress browser-owned prompts and bubbles."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    current = target
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


_SUPPRESSED_PROMPT_PREFERENCES: tuple[tuple[tuple[str, ...], object], ...] = (
    # Chromium's supported password-manager switches. Disabling the service
    # prevents the post-login "Save password" bubble while the tool continues
    # to retrieve credentials from Windows Credential Manager.
    (("credentials_enable_service",), False),
    (("credentials_enable_autosignin",), False),
    (("profile", "password_manager_enabled"), False),
    # Block browser-owned permission prompts silently. These do not alter
    # status/toast elements rendered by the game itself.
    (("profile", "default_content_setting_values", "notifications"), 2),
    (("profile", "default_content_setting_values", "geolocation"), 2),
    (("profile", "default_content_setting_values", "media_stream_mic"), 2),
    (("profile", "default_content_setting_values", "media_stream_camera"), 2),
    (("profile", "default_content_setting_values", "popups"), 2),
    (("autofill", "profile_enabled"), False),
    (("autofill", "credit_card_enabled"), False),
    (("translate", "enabled"), False),
    (("browser", "check_default_browser"), False),
    (("browser", "has_seen_welcome_page"), True),
    # Do not show Chrome's restore-session bubble after a forced shutdown.
    (("profile", "exit_type"), "Normal"),
    (("profile", "exited_cleanly"), True),
)


def suppress_browser_prompts(user_data_dir: Path) -> Path:
    """Merge prompt-suppression preferences without deleting profile state."""
    preferences = Path(user_data_dir) / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    if preferences.exists():
        try:
            loaded = json.loads(preferences.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # Chrome itself recreates malformed Preferences. Keeping this
            # operation best-effort must never make an otherwise valid profile
            # impossible to open.
            payload = {}
    for path, value in _SUPPRESSED_PROMPT_PREFERENCES:
        _set_nested(payload, path, value)
    temporary = preferences.with_name("Preferences.ik-auto.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, preferences)
    return preferences
