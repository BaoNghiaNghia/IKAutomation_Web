from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ik_chrome_auto.web_control import (
    CommandEnvelope,
    CommandKind,
    DeviceLease,
    DeviceRunState,
    DeviceSnapshot,
    FarmProfile,
    SnapshotProjector,
    UserRole,
    transition,
)


NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)


def profile(**overrides: object) -> FarmProfile:
    values: dict[str, object] = {
        "profile_id": "profile-1",
        "name": "Farm default",
        "resources": ("iron", "stone"),
        "resource_priority": ("iron", "stone"),
        "level_priority": (7, 6, 5),
        "allowed_teams": (2, 3),
        "team_priority": (3, 2),
        "allow_team_1": False,
        "ready_check_interval_seconds": 900,
        "ready_max_wait_seconds": 43_200,
        "version": 1,
    }
    values.update(overrides)
    return FarmProfile(**values)  # type: ignore[arg-type]


def test_profile_validation_rejects_team_one_without_explicit_permission() -> None:
    with pytest.raises(ValueError, match="Team 1"):
        profile(allowed_teams=(1, 2), team_priority=(1, 2)).validate()


def test_operator_can_start_but_viewer_cannot() -> None:
    command = CommandEnvelope(
        command_id="command-1",
        idempotency_key="run-1",
        kind=CommandKind.FARM_START,
        actor_id="operator-1",
        actor_role=UserRole.OPERATOR,
        agent_id="agent-1",
        device_ids=("device-1",),
        expected_version=0,
        requested_at=NOW,
        deadline_at=NOW + timedelta(minutes=1),
    )
    command.validate(NOW)

    with pytest.raises(PermissionError):
        replace(command, actor_role=UserRole.VIEWER).validate(NOW)


def snapshot(event_id: str, sequence: int) -> DeviceSnapshot:
    return DeviceSnapshot(
        event_id=event_id,
        agent_id="agent-1",
        run_id="run-1",
        device_id="device-1",
        sequence=sequence,
        state=DeviceRunState.WAITING,
        occurred_at=NOW,
    )


def test_projector_discards_duplicate_and_stale_snapshots() -> None:
    projector = SnapshotProjector()
    assert projector.apply(snapshot("event-2", 2))
    assert not projector.apply(snapshot("event-2", 2))
    assert not projector.apply(snapshot("event-1", 1))
    assert projector.latest("run-1", "device-1").sequence == 2  # type: ignore[union-attr]


def test_lease_only_permits_its_live_owner() -> None:
    lease = DeviceLease("lease-1", "agent-1", "device-1", "run-1", NOW + timedelta(seconds=10))
    assert lease.permits_input(agent_id="agent-1", device_id="device-1", device_run_id="run-1", now=NOW)
    assert not lease.permits_input(agent_id="agent-2", device_id="device-1", device_run_id="run-1", now=NOW)
    assert not lease.permits_input(
        agent_id="agent-1", device_id="device-1", device_run_id="run-1", now=NOW + timedelta(seconds=10)
    )


def test_state_machine_rejects_blind_input_path_from_unknown_state() -> None:
    assert transition(DeviceRunState.PREFLIGHT, DeviceRunState.READY) == DeviceRunState.READY
    with pytest.raises(ValueError):
        transition(DeviceRunState.PREFLIGHT, DeviceRunState.RUNNING)
