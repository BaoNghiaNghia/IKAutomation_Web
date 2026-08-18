"""Framework-independent domain rules for the web control plane.

The web application owns intent, validation, audit and read models.  It never
owns an ADB handle or translates a browser click into a game input; that work
belongs exclusively to the Windows Agent after it has acquired a device lease.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class UserRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class DeviceRunState(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    RECOVERING = "recovering"
    QUARANTINED = "quarantined"
    STOPPED = "stopped"


class CommandKind(StrEnum):
    FARM_START = "farm.start"
    FARM_STOP = "farm.stop"
    DEVICE_PAUSE = "device.pause"
    DEVICE_RESUME = "device.resume"
    DEVICE_QUARANTINE = "device.quarantine"
    DEVICE_RECOVER = "device.recover"
    DIAGNOSTIC_CAPTURE = "diagnostic.capture"


ALLOWED_TRANSITIONS: dict[DeviceRunState, frozenset[DeviceRunState]] = {
    DeviceRunState.QUEUED: frozenset({DeviceRunState.PREFLIGHT, DeviceRunState.STOPPED}),
    DeviceRunState.PREFLIGHT: frozenset(
        {DeviceRunState.READY, DeviceRunState.RECOVERING, DeviceRunState.STOPPED}
    ),
    DeviceRunState.READY: frozenset(
        {DeviceRunState.RUNNING, DeviceRunState.WAITING, DeviceRunState.STOPPED}
    ),
    DeviceRunState.RUNNING: frozenset(
        {DeviceRunState.WAITING, DeviceRunState.RECOVERING, DeviceRunState.STOPPED}
    ),
    DeviceRunState.WAITING: frozenset({DeviceRunState.PREFLIGHT, DeviceRunState.STOPPED}),
    DeviceRunState.RECOVERING: frozenset(
        {DeviceRunState.PREFLIGHT, DeviceRunState.QUARANTINED, DeviceRunState.STOPPED}
    ),
    DeviceRunState.QUARANTINED: frozenset(
        {DeviceRunState.RECOVERING, DeviceRunState.STOPPED}
    ),
    DeviceRunState.STOPPED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class FarmProfile:
    profile_id: str
    name: str
    resources: tuple[str, ...]
    resource_priority: tuple[str, ...]
    level_priority: tuple[int, ...]
    allowed_teams: tuple[int, ...]
    team_priority: tuple[int, ...]
    allow_team_1: bool
    ready_check_interval_seconds: int
    ready_max_wait_seconds: int
    version: int

    def validate(self) -> None:
        if len(set(self.resources)) < 2:
            raise ValueError("Farm profile phải chọn ít nhất hai loại tài nguyên")
        if set(self.resource_priority) != set(self.resources):
            raise ValueError("resource_priority phải chứa đúng các resource đã chọn")
        if not self.level_priority or len(set(self.level_priority)) != len(self.level_priority):
            raise ValueError("level_priority phải không rỗng và không trùng")
        if any(level < 1 or level > 20 for level in self.level_priority):
            raise ValueError("level_priority ngoài phạm vi game hợp lệ")
        if not self.allowed_teams or len(set(self.allowed_teams)) != len(self.allowed_teams):
            raise ValueError("allowed_teams phải không rỗng và không trùng")
        if 1 in self.allowed_teams and not self.allow_team_1:
            raise ValueError("Team 1 yêu cầu allow_team_1=true")
        if set(self.team_priority) != set(self.allowed_teams):
            raise ValueError("team_priority phải chứa đúng allowed_teams")
        if self.ready_check_interval_seconds <= 0:
            raise ValueError("ready_check_interval_seconds phải lớn hơn 0")
        if self.ready_max_wait_seconds < self.ready_check_interval_seconds:
            raise ValueError("ready_max_wait_seconds phải lớn hơn hoặc bằng interval")
        if self.version < 1:
            raise ValueError("version phải lớn hơn 0")


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    command_id: str
    idempotency_key: str
    kind: CommandKind
    actor_id: str
    actor_role: UserRole
    agent_id: str
    device_ids: tuple[str, ...]
    expected_version: int
    requested_at: datetime
    deadline_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self, now: datetime) -> None:
        if not self.command_id or not self.idempotency_key or not self.actor_id:
            raise ValueError("command_id, idempotency_key và actor_id là bắt buộc")
        if not self.agent_id or not self.device_ids:
            raise ValueError("agent_id và device_ids là bắt buộc")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("device_ids không được trùng")
        if self.expected_version < 0:
            raise ValueError("expected_version không hợp lệ")
        if self.requested_at.tzinfo is None or self.deadline_at.tzinfo is None:
            raise ValueError("command timestamps phải có timezone")
        if self.deadline_at <= now.astimezone(UTC):
            raise ValueError("command đã hết hạn")
        if self.kind in {CommandKind.FARM_START, CommandKind.FARM_STOP}:
            self._require(UserRole.OPERATOR)
        elif self.kind in {CommandKind.DEVICE_QUARANTINE, CommandKind.DEVICE_RECOVER}:
            self._require(UserRole.ADMIN)
        else:
            self._require(UserRole.OPERATOR)

    def _require(self, minimum: UserRole) -> None:
        rank = {UserRole.VIEWER: 0, UserRole.OPERATOR: 1, UserRole.ADMIN: 2}
        if rank[self.actor_role] < rank[minimum]:
            raise PermissionError(f"Role {self.actor_role} không có quyền gửi {self.kind}")


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    event_id: str
    agent_id: str
    run_id: str
    device_id: str
    sequence: int
    state: DeviceRunState
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.event_id or not self.agent_id or not self.run_id or not self.device_id:
            raise ValueError("snapshot phải có đầy đủ identity")
        if self.sequence < 0:
            raise ValueError("sequence không được âm")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at phải có timezone")


@dataclass(slots=True)
class SnapshotProjector:
    """Applies an at-least-once event stream without stale UI regressions."""

    _seen_event_ids: set[str] = field(default_factory=set)
    _latest: dict[tuple[str, str], DeviceSnapshot] = field(default_factory=dict)

    def apply(self, snapshot: DeviceSnapshot) -> bool:
        snapshot.validate()
        if snapshot.event_id in self._seen_event_ids:
            return False
        key = (snapshot.run_id, snapshot.device_id)
        existing = self._latest.get(key)
        self._seen_event_ids.add(snapshot.event_id)
        if existing is not None and snapshot.sequence <= existing.sequence:
            return False
        self._latest[key] = snapshot
        return True

    def latest(self, run_id: str, device_id: str) -> DeviceSnapshot | None:
        return self._latest.get((run_id, device_id))


@dataclass(frozen=True, slots=True)
class DeviceLease:
    lease_id: str
    agent_id: str
    device_id: str
    device_run_id: str
    expires_at: datetime

    def permits_input(
        self, *, agent_id: str, device_id: str, device_run_id: str, now: datetime
    ) -> bool:
        """The agent must call this immediately before a production input."""
        return (
            self.agent_id == agent_id
            and self.device_id == device_id
            and self.device_run_id == device_run_id
            and self.expires_at > now.astimezone(UTC)
        )


def transition(current: DeviceRunState, target: DeviceRunState) -> DeviceRunState:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Không thể chuyển device run từ {current} sang {target}")
    return target
