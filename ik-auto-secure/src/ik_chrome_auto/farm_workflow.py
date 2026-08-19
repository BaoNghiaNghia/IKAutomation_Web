"""Bounded, template-gated farm workflow for browser game profiles.

This module intentionally decides *what may happen next*; it never contains
screen coordinates.  A browser adapter must first verify every state/template
and then execute the matching guarded input.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FarmGameState(StrEnum):
    UNKNOWN = "unknown"
    CITY = "city"
    WORLD_MAP = "world_map"
    RESOURCE_SEARCH = "resource_search"
    RESOURCE_POPUP = "resource_popup"
    TEAM_SELECTION = "team_selection"
    STORAGE_LIMIT = "storage_limit"
    RESOURCE_EXPIRY = "resource_expiry"


class FarmStep(StrEnum):
    PREFLIGHT = "preflight"
    ENTER_WORLD_MAP = "enter_world_map"
    CHECK_TEAMS = "check_teams"
    OPEN_SEARCH = "open_search"
    FIND_RESOURCE = "find_resource"
    OPEN_TEAM_SELECTION = "open_team_selection"
    SELECT_TEAM = "select_team"
    DISPATCH = "dispatch"
    WAITING = "waiting"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class FarmPolicy:
    resources: tuple[str, ...] = ("iron", "stone", "wood", "food")
    levels: tuple[int, ...] = (7, 6, 5)
    allowed_teams: tuple[int, ...] = (2, 3, 4, 5)
    retry_delay_seconds: int = 30


@dataclass(frozen=True, slots=True)
class FarmDecision:
    step: FarmStep
    message: str
    resource: str | None = None
    level: int | None = None
    team: int | None = None
    input_allowed: bool = False


class FarmWorkflow:
    """One-dispatch-per-cycle decision state machine.

    `input_allowed` only becomes true after a caller has supplied the expected
    verified state.  The browser layer must additionally rematch the target
    immediately before and verify a post-condition immediately after input.
    """

    def __init__(self, policy: FarmPolicy | None = None) -> None:
        self.policy = policy or FarmPolicy()
        self.step = FarmStep.PREFLIGHT
        self.resource_index = 0
        self.level_index = 0
        self.team: int | None = None

    def decide(
        self,
        state: FarmGameState,
        *,
        ready_teams: tuple[int, ...] = (),
        target_verified: bool = False,
        dispatch_verified: bool = False,
    ) -> FarmDecision:
        if state == FarmGameState.UNKNOWN:
            self.step = FarmStep.PREFLIGHT
            return FarmDecision(self.step, "Chưa nhận diện được game; không thao tác")
        if state in {FarmGameState.STORAGE_LIMIT, FarmGameState.RESOURCE_EXPIRY}:
            self.step = FarmStep.WAITING
            return FarmDecision(self.step, "Kho đầy hoặc tài nguyên hết hạn; chờ lượt tiếp theo")
        if self.step == FarmStep.PREFLIGHT:
            if state == FarmGameState.CITY:
                self.step = FarmStep.ENTER_WORLD_MAP
                return FarmDecision(self.step, "Đã nhận diện City; cần mở World Map", input_allowed=target_verified)
            if state == FarmGameState.WORLD_MAP:
                self.step = FarmStep.CHECK_TEAMS
            else:
                return FarmDecision(self.step, "Chờ City hoặc World Map đã được xác minh")
        if self.step == FarmStep.ENTER_WORLD_MAP:
            if state != FarmGameState.WORLD_MAP:
                return FarmDecision(self.step, "Chờ xác minh World Map sau thao tác")
            self.step = FarmStep.CHECK_TEAMS
        if self.step == FarmStep.CHECK_TEAMS:
            allowed = tuple(team for team in self.policy.allowed_teams if team in ready_teams)
            if not allowed:
                self.step = FarmStep.WAITING
                return FarmDecision(self.step, "Không có đội sẵn sàng; chờ lượt tiếp theo")
            self.team = allowed[0]
            self.step = FarmStep.OPEN_SEARCH
            return FarmDecision(self.step, f"Đội {self.team} sẵn sàng; mở tìm tài nguyên", team=self.team, input_allowed=target_verified)
        resource, level = self._target()
        if self.step == FarmStep.OPEN_SEARCH:
            if state != FarmGameState.RESOURCE_SEARCH:
                return FarmDecision(self.step, "Chờ panel tìm tài nguyên được xác minh")
            self.step = FarmStep.FIND_RESOURCE
            return FarmDecision(self.step, f"Tìm {resource} cấp {level}", resource, level, self.team, target_verified)
        if self.step == FarmStep.FIND_RESOURCE:
            if state != FarmGameState.RESOURCE_POPUP:
                return FarmDecision(self.step, "Không thấy tài nguyên; chuyển phương án tìm kiếm")
            self.step = FarmStep.OPEN_TEAM_SELECTION
            return FarmDecision(self.step, "Mở chọn đội", resource, level, self.team, target_verified)
        if self.step == FarmStep.OPEN_TEAM_SELECTION:
            if state != FarmGameState.TEAM_SELECTION:
                return FarmDecision(self.step, "Chờ panel chọn đội được xác minh")
            self.step = FarmStep.SELECT_TEAM
            return FarmDecision(self.step, f"Chọn đội {self.team}", resource, level, self.team, target_verified)
        if self.step == FarmStep.SELECT_TEAM:
            self.step = FarmStep.DISPATCH
            return FarmDecision(self.step, "Điều quân", resource, level, self.team, target_verified)
        if self.step == FarmStep.DISPATCH:
            if not dispatch_verified:
                return FarmDecision(self.step, "Chờ xác minh đoàn quân đã xuất phát")
            self.step = FarmStep.WAITING
            return FarmDecision(self.step, "Đã điều một đội; chờ cycle tiếp theo", resource, level, self.team)
        return FarmDecision(self.step, "Đang chờ lượt farm tiếp theo")

    def advance_search_plan(self) -> bool:
        """Try the next level then resource; false means this cycle is exhausted."""
        self.level_index += 1
        if self.level_index < len(self.policy.levels):
            self.step = FarmStep.OPEN_SEARCH
            return True
        self.level_index = 0
        self.resource_index += 1
        if self.resource_index < len(self.policy.resources):
            self.step = FarmStep.OPEN_SEARCH
            return True
        self.step = FarmStep.WAITING
        return False

    def _target(self) -> tuple[str, int]:
        return self.policy.resources[self.resource_index], self.policy.levels[self.level_index]
