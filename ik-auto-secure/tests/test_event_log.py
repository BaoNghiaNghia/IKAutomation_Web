from __future__ import annotations

from ik_chrome_auto.event_log import JsonLineLog


def test_json_line_log_rotates_at_size_limit(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    log = JsonLineLog(path, max_bytes=80, backups=1)

    log.write("first", {"value": "a" * 40})
    log.write("second", {"value": "b" * 40})

    assert path.exists()
    assert path.with_name("events.jsonl.1").exists()
    assert '"event": "second"' in path.read_text(encoding="utf-8")
    assert '"event": "first"' in path.with_name("events.jsonl.1").read_text(encoding="utf-8")
