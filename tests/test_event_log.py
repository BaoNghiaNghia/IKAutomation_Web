from __future__ import annotations

from pathlib import Path

from ik_chrome_auto.event_log import (
    JsonLineLog,
    migrate_legacy_profile_log,
    profile_log_path,
)


def test_json_line_log_rotates_at_size_limit(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = JsonLineLog(path, max_bytes=80, backups=1)

    log.write("first", {"value": "a" * 40})
    log.write("second", {"value": "b" * 40})

    assert path.exists()
    assert path.with_name("events.jsonl.1").exists()
    assert '"event": "second"' in path.read_text(encoding="utf-8")
    assert '"event": "first"' in path.with_name("events.jsonl.1").read_text(encoding="utf-8")


def test_profile_log_path_uses_the_profile_display_name() -> None:
    path = profile_log_path(
        Path("logs"),
        "farm",
        "Tài khoản 02 · nam139***...",
        "account-3",
    )

    assert path.name == "farm-Tài khoản 02 - nam139＊＊＊….jsonl"


def test_legacy_profile_log_is_renamed_without_losing_history(tmp_path) -> None:
    legacy = tmp_path / "farm-account-3.jsonl"
    legacy.write_text('{"event":"started"}\n', encoding="utf-8")
    target = profile_log_path(
        tmp_path,
        "farm",
        "Tài khoản 02 · nam139***...",
        "account-3",
    )

    migrate_legacy_profile_log(legacy, target)

    assert not legacy.exists()
    assert target.read_text(encoding="utf-8") == '{"event":"started"}\n'
