from __future__ import annotations

import json
from pathlib import Path

from ik_chrome_auto import startup_update
from ik_chrome_auto.event_log import JsonLineLog, _safe_payload
from ik_chrome_auto.update_check import GitUpdateCheckError, GitUpdateStatus


def _setup(monkeypatch, tmp_path: Path, *, behind: int, dirty: bool) -> list[object]:
    script = tmp_path / "scripts" / "update-on-launch.ps1"
    script.parent.mkdir()
    script.write_text("# updater", encoding="utf-8")
    status = GitUpdateStatus(tmp_path, "main", "a" * 40, "b" * 40, behind)
    monkeypatch.setattr(startup_update, "find_git_workspace", lambda _p: tmp_path)
    monkeypatch.setattr(startup_update, "check_for_git_updates", lambda _p: status)
    monkeypatch.setattr(startup_update.os, "name", "nt")
    monkeypatch.delenv("IK_AUTO_SKIP_STARTUP_UPDATE", raising=False)
    calls: list[object] = []

    def fake_git(_root: Path, *args: str) -> str:
        calls.append(args)
        if args[:1] == ("status",):
            return " M example.py" if dirty else ""
        if args[:1] == ("branch",):
            return "main"
        if args[:1] == ("rev-parse",):
            return "origin/main"
        return ""

    monkeypatch.setattr(startup_update, "_git", fake_git)
    monkeypatch.setattr(startup_update.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_updater_skips_dirty_tree_without_spawning(monkeypatch, tmp_path: Path) -> None:
    calls = _setup(monkeypatch, tmp_path, behind=2, dirty=True)

    assert startup_update.maybe_start_update(tmp_path / "config.json", frozen=False) is False
    assert not any(isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict) for item in calls)


def test_updater_schedules_only_when_clean_and_behind(monkeypatch, tmp_path: Path) -> None:
    calls = _setup(monkeypatch, tmp_path, behind=3, dirty=False)

    assert startup_update.maybe_start_update(tmp_path / "config.json", frozen=True) is True
    spawned = [item for item in calls if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict)]
    assert len(spawned) == 1
    assert "-ParentPid" in spawned[0][0][0]
    assert "-LaunchMode" in spawned[0][0][0]


def test_updater_no_changes_no_spawn(monkeypatch, tmp_path: Path) -> None:
    calls = _setup(monkeypatch, tmp_path, behind=0, dirty=False)

    assert startup_update.maybe_start_update(tmp_path / "config.json") is False
    assert not any(isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict) for item in calls)


def test_updater_network_failure_opens_existing_app(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path, behind=2, dirty=False)

    def unavailable(_root: Path) -> None:
        raise GitUpdateCheckError("network unavailable")

    monkeypatch.setattr(startup_update, "check_for_git_updates", unavailable)
    assert startup_update.maybe_start_update(tmp_path / "config.json") is False


def test_jsonl_redacts_credentials_and_url_tokens(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = JsonLineLog(path)
    log.write("diagnostics", {
        "password": "supersecret",
        "detail": "Bearer supersecret2 URL https://example.com/x?token=supersecret3",
        "nested": {"api_key": "supersecret4"},
    })
    data = json.loads(path.read_text(encoding="utf-8"))
    for secret in ("supersecret", "supersecret2", "supersecret3", "supersecret4"):
        assert secret not in path.read_text(encoding="utf-8")
    assert data["password"] == "[REDACTED]"
    assert data["nested"]["api_key"] == "[REDACTED]"
    assert data["event"] == "diagnostics"
    assert _safe_payload({"access_token": "abc"})["access_token"] == "[REDACTED]"