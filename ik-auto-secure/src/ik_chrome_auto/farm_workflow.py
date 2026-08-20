"""Bounded, template-gated farm workflow for browser game profiles.

This module intentionally decides *what may happen next*; it never contains
screen coordinates.  A browser adapter must first verify every state/template
and then execute the matching guarded input.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from random import SystemRandom


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
    RETURN_TO_CITY = "return_to_city"
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
    # Only these levels have an approved World Map relocation pool. Level 5
    # must not enter that flow because no verified area rule exists for it.
    levels: tuple[int, ...] = (6, 7, 8)
    # All four rows can be used. The selected team is the first verified Ready
    # row from the World Map roster and remains locked for the whole cycle.
    allowed_teams: tuple[int, ...] = (1, 2, 3, 4)
    # A completed march is followed by a fresh roster scan after 15 seconds.
    # This is deliberately short enough to pick up another ready team while
    # still allowing the game HUD and dispatch animation to settle.
    retry_delay_seconds: int = 15


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

    def __init__(self, policy: FarmPolicy | None = None, *, resource_order: tuple[str, ...] | None = None) -> None:
        self.policy = policy or FarmPolicy()
        # One shuffled resource order is fixed for the whole cycle. A miss
        # changes resource first; only after all four were tried does the
        # runner invoke the approved area-relocation flow for this level.
        self.resource_order = resource_order or tuple(SystemRandom().sample(self.policy.resources, len(self.policy.resources)))
        self.step = FarmStep.PREFLIGHT
        self.resource_index = 0
        self.level_index = 0
        self.team: int | None = None
        # WAITING has several meanings. Only the no-ready-team wait may be
        # resumed directly from a fresh World Map roster scan; post-dispatch
        # and terminal search-plan waits must still start a new cycle.
        self.waiting_for_ready_team = False

    def decide(
        self,
        state: FarmGameState,
        *,
        ready_teams: tuple[int, ...] = (),
        target_verified: bool = False,
        team_selected: bool = False,
        dispatch_verified: bool = False,
    ) -> FarmDecision:
        if state == FarmGameState.UNKNOWN:
            self.step = FarmStep.PREFLIGHT
            self.waiting_for_ready_team = False
            return FarmDecision(self.step, "Chưa nhận diện được game; không thao tác")
        if state in {FarmGameState.STORAGE_LIMIT, FarmGameState.RESOURCE_EXPIRY}:
            self.step = FarmStep.WAITING
            self.waiting_for_ready_team = False
            return FarmDecision(self.step, "Kho đầy hoặc tài nguyên hết hạn; chờ lượt tiếp theo")
        if self.step == FarmStep.WAITING and self.waiting_for_ready_team:
            if state != FarmGameState.WORLD_MAP or not ready_teams:
                return FarmDecision(self.step, "Không có đội sẵn sàng; chờ lượt tiếp theo")
            # The runner scans the World Map again after the bounded 15-second
            # delay. Resume the interrupted initial cycle as soon as that
            # fresh scan contains at least one verified Ready row.
            self.step = FarmStep.CHECK_TEAMS
            self.waiting_for_ready_team = False
        if self.step == FarmStep.PREFLIGHT:
            if state == FarmGameState.CITY:
                self.step = FarmStep.ENTER_WORLD_MAP
                return FarmDecision(self.step, "Đã nhận diện City; cần mở World Map", input_allowed=target_verified)
            if state == FarmGameState.WORLD_MAP:
                # Every browser cycle starts from City. This clears stale
                # World Map panels and makes the City → World Map transition
                # explicit before we inspect teams or search resources.
                self.step = FarmStep.RETURN_TO_CITY
                return FarmDecision(self.step, "Đang ở World Map; cần về City trước khi farm")
            else:
                return FarmDecision(self.step, "Chờ City hoặc World Map đã được xác minh")
        if self.step == FarmStep.RETURN_TO_CITY:
            if state != FarmGameState.CITY:
                return FarmDecision(self.step, "Chờ xác minh City sau thao tác quay về")
            self.step = FarmStep.ENTER_WORLD_MAP
            return FarmDecision(self.step, "Đã xác minh City; cần mở World Map", input_allowed=target_verified)
        if self.step == FarmStep.ENTER_WORLD_MAP:
            if state != FarmGameState.WORLD_MAP:
                return FarmDecision(self.step, "Chờ xác minh World Map sau thao tác")
            self.step = FarmStep.CHECK_TEAMS
        if self.step == FarmStep.CHECK_TEAMS:
            allowed = tuple(team for team in self.policy.allowed_teams if team in ready_teams)
            if not allowed:
                self.step = FarmStep.WAITING
                self.waiting_for_ready_team = True
                return FarmDecision(self.step, "Không có đội sẵn sàng; chờ lượt tiếp theo")
            # Do not replace a team chosen from the cycle's initial World Map
            # scan just because an intermediate frame is reclassified. That
            # old behaviour made the browser flow look like it defaulted to
            # team 2. A new cycle creates a new workflow and may choose again.
            if self.team is None:
                self.team = allowed[0]
            self.waiting_for_ready_team = False
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
                return FarmDecision(
                    self.step,
                    "Chờ tài nguyên xuất hiện; giữ nguyên phương án tìm kiếm",
                    resource,
                    level,
                    self.team,
                )
            self.step = FarmStep.OPEN_TEAM_SELECTION
            return FarmDecision(self.step, "Mở chọn đội", resource, level, self.team, target_verified)
        if self.step == FarmStep.OPEN_TEAM_SELECTION:
            if state != FarmGameState.TEAM_SELECTION:
                return FarmDecision(self.step, "Chờ panel chọn đội được xác minh")
            self.step = FarmStep.SELECT_TEAM
            return FarmDecision(self.step, f"Chọn đội {self.team}", resource, level, self.team, target_verified)
        if self.step == FarmStep.SELECT_TEAM:
            if state != FarmGameState.TEAM_SELECTION or not team_selected:
                return FarmDecision(
                    self.step,
                    f"Chờ xác minh đội {self.team} đã được chọn",
                    resource,
                    level,
                    self.team,
                )
            self.step = FarmStep.DISPATCH
            return FarmDecision(self.step, "Điều quân", resource, level, self.team, target_verified)
        if self.step == FarmStep.DISPATCH:
            if not dispatch_verified:
                return FarmDecision(self.step, "Chờ xác minh đoàn quân đã xuất phát")
            self.step = FarmStep.WAITING
            self.waiting_for_ready_team = False
            return FarmDecision(self.step, "Đã điều một đội; chờ cycle tiếp theo", resource, level, self.team)
        return FarmDecision(self.step, "Đang chờ lượt farm tiếp theo")

    def advance_search_plan(self) -> bool:
        """Try the next resource; false means this four-resource round ended."""
        self.resource_index += 1
        if self.resource_index < len(self.resource_order):
            self.step = FarmStep.OPEN_SEARCH
            return True
        self.resource_index = 0
        return False

    def advance_level_plan(self) -> bool:
        """Move to the next approved level after its area point pool ends."""
        self.level_index += 1
        if self.level_index < len(self.policy.levels):
            self.step = FarmStep.OPEN_SEARCH
            return True
        self.step = FarmStep.WAITING
        self.waiting_for_ready_team = False
        return False

    def current_target(self) -> tuple[str, int]:
        return self._target()

    def _target(self) -> tuple[str, int]:
        return self.resource_order[self.resource_index], self.policy.levels[self.level_index]
