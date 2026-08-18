from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ik_chrome_auto.control_store import SqliteControlStore
from ik_chrome_auto.web_control import CommandEnvelope, CommandKind, DeviceRunState, DeviceSnapshot, UserRole


NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)


def command(*, key: str = "key-1", version: int = 1) -> CommandEnvelope:
    return CommandEnvelope(
        command_id="command-1", idempotency_key=key, kind=CommandKind.FARM_START,
        actor_id="operator-1", actor_role=UserRole.OPERATOR, agent_id="agent-1",
        device_ids=("device-1",), expected_version=version, requested_at=NOW,
        deadline_at=NOW + timedelta(minutes=1), payload={"profileId": "profile-1"},
    )


def snapshot(event_id: str, sequence: int) -> DeviceSnapshot:
    return DeviceSnapshot(event_id, "agent-1", "run-1", "device-1", sequence, DeviceRunState.WAITING, NOW)


def test_command_is_durable_idempotent_and_audited(tmp_path) -> None:
    store = SqliteControlStore(tmp_path / "control.db")
    store.register_device("device-1", "agent-1", version=1)

    first = store.submit_command(command(), NOW)
    duplicate = store.submit_command(command(), NOW)

    assert first.accepted and not first.duplicate
    assert duplicate.duplicate and duplicate.command_id == first.command_id
    assert [row["action"] for row in store.audit_entries()] == ["farm.start"]


def test_command_rejects_wrong_agent_or_version(tmp_path) -> None:
    store = SqliteControlStore(tmp_path / "control.db")
    store.register_device("device-1", "agent-1", version=1)

    with pytest.raises(RuntimeError, match="version"):
        store.submit_command(command(version=0), NOW)
    with pytest.raises(PermissionError, match="không thuộc"):
        store.submit_command(replace(command(), agent_id="agent-2"), NOW)


def test_event_dedup_and_sequence_are_persistent(tmp_path) -> None:
    store = SqliteControlStore(tmp_path / "control.db")
    store.register_device("device-1", "agent-1")

    assert store.record_snapshot(snapshot("event-2", 2))
    assert not store.record_snapshot(snapshot("event-2", 2))
    assert not store.record_snapshot(snapshot("event-1", 1))
    assert store.latest_snapshot("run-1", "device-1").sequence == 2  # type: ignore[union-attr]
