"""Framework-independent contracts for browser-profile farming."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class UserRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class ProfileRunState(StrEnum):
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
    PROFILE_PAUSE = "profile.pause"
    PROFILE_RESUME = "profile.resume"
    PROFILE_QUARANTINE = "profile.quarantine"
    PROFILE_RECOVER = "profile.recover"
    DIAGNOSTIC_CAPTURE = "diagnostic.capture"


ALLOWED_TRANSITIONS: dict[ProfileRunState, frozenset[ProfileRunState]] = {
    ProfileRunState.QUEUED: frozenset({ProfileRunState.PREFLIGHT, ProfileRunState.STOPPED}),
    ProfileRunState.PREFLIGHT: frozenset(
        {ProfileRunState.READY, ProfileRunState.RECOVERING, ProfileRunState.STOPPED}
    ),
    ProfileRunState.READY: frozenset(
        {ProfileRunState.RUNNING, ProfileRunState.WAITING, ProfileRunState.RECOVERING, ProfileRunState.STOPPED}
    ),
    ProfileRunState.RUNNING: frozenset(
        {ProfileRunState.WAITING, ProfileRunState.RECOVERING, ProfileRunState.STOPPED}
    ),
    ProfileRunState.WAITING: frozenset(
        {ProfileRunState.PREFLIGHT, ProfileRunState.RECOVERING, ProfileRunState.STOPPED}
    ),
    ProfileRunState.RECOVERING: frozenset(
        {ProfileRunState.PREFLIGHT, ProfileRunState.QUARANTINED, ProfileRunState.STOPPED}
    ),
    ProfileRunState.QUARANTINED: frozenset(
        {ProfileRunState.RECOVERING, ProfileRunState.STOPPED}
    ),
    ProfileRunState.STOPPED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class FarmProfile:
    profile_id: str
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
        if len(set(self.resources)) < 2 or set(self.resource_priority) != set(self.resources):
            raise ValueError("resource policy phải chọn và ưu tiên ít nhất hai resource")
        if not self.level_priority or len(set(self.level_priority)) != len(self.level_priority):
            raise ValueError("level_priority phải không rỗng và không trùng")
        if not self.allowed_teams or set(self.team_priority) != set(self.allowed_teams):
            raise ValueError("team policy không hợp lệ")
        if 1 in self.allowed_teams and not self.allow_team_1:
            raise ValueError("Team 1 yêu cầu allow_team_1=true")
        if self.ready_check_interval_seconds <= 0 or self.ready_max_wait_seconds < self.ready_check_interval_seconds:
            raise ValueError("ready timing không hợp lệ")
        if self.version < 1:
            raise ValueError("version phải lớn hơn 0")


@dataclass(frozen=True, slots=True)
class BrowserCommand:
    command_id: str
    idempotency_key: str
    kind: CommandKind
    actor_id: str
    actor_role: UserRole
    worker_id: str
    browser_profile_ids: tuple[str, ...]
    expected_version: int
    requested_at: datetime
    deadline_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self, now: datetime) -> None:
        if not self.command_id or not self.idempotency_key or not self.actor_id or not self.worker_id:
            raise ValueError("command identity là bắt buộc")
        if not self.browser_profile_ids or len(set(self.browser_profile_ids)) != len(self.browser_profile_ids):
            raise ValueError("browser_profile_ids không hợp lệ")
        if self.expected_version < 0 or self.requested_at.tzinfo is None or self.deadline_at.tzinfo is None:
            raise ValueError("command version/timestamp không hợp lệ")
        if self.deadline_at <= now.astimezone(UTC):
            raise ValueError("command đã hết hạn")
        required = UserRole.ADMIN if self.kind in {CommandKind.PROFILE_QUARANTINE, CommandKind.PROFILE_RECOVER} else UserRole.OPERATOR
        rank = {UserRole.VIEWER: 0, UserRole.OPERATOR: 1, UserRole.ADMIN: 2}
        if rank[self.actor_role] < rank[required]:
            raise PermissionError(f"Role {self.actor_role} không có quyền gửi {self.kind}")


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    event_id: str
    worker_id: str
    run_id: str
    browser_profile_id: str
    sequence: int
    state: ProfileRunState
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not all((self.event_id, self.worker_id, self.run_id, self.browser_profile_id)):
            raise ValueError("snapshot identity là bắt buộc")
        if self.sequence < 0 or self.occurred_at.tzinfo is None:
            raise ValueError("snapshot sequence/timestamp không hợp lệ")


@dataclass(frozen=True, slots=True)
class ProfileLease:
    lease_id: str
    worker_id: str
    browser_profile_id: str
    profile_run_id: str
    generation: int
    expires_at: datetime

    def permits_browser_input(self, *, worker_id: str, browser_profile_id: str, profile_run_id: str, now: datetime) -> bool:
        return (
            self.worker_id == worker_id and self.browser_profile_id == browser_profile_id
            and self.profile_run_id == profile_run_id and self.expires_at > now.astimezone(UTC)
        )


def transition(current: ProfileRunState, target: ProfileRunState) -> ProfileRunState:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Không thể chuyển profile run từ {current} sang {target}")
    return target
