from __future__ import annotations

from ik_chrome_auto.farm_workflow import FarmGameState, FarmStep, FarmWorkflow


def test_unknown_state_never_allows_input() -> None:
    decision = FarmWorkflow().decide(FarmGameState.UNKNOWN, target_verified=True)
    assert decision.step == FarmStep.PREFLIGHT
    assert decision.input_allowed is False


def test_happy_path_completes_exactly_one_verified_dispatch() -> None:
    farm = FarmWorkflow()
    assert farm.decide(FarmGameState.CITY).step == FarmStep.ENTER_WORLD_MAP
    assert farm.decide(FarmGameState.WORLD_MAP, ready_teams=(2,)).step == FarmStep.OPEN_SEARCH
    assert farm.decide(FarmGameState.RESOURCE_SEARCH, target_verified=True).step == FarmStep.FIND_RESOURCE
    assert farm.decide(FarmGameState.RESOURCE_POPUP, target_verified=True).step == FarmStep.OPEN_TEAM_SELECTION
    assert farm.decide(FarmGameState.TEAM_SELECTION, target_verified=True).step == FarmStep.SELECT_TEAM
    assert farm.decide(FarmGameState.TEAM_SELECTION, target_verified=True).step == FarmStep.SELECT_TEAM
    assert farm.decide(FarmGameState.TEAM_SELECTION, team_selected=True).step == FarmStep.DISPATCH
    assert farm.decide(FarmGameState.TEAM_SELECTION, dispatch_verified=True).step == FarmStep.WAITING


def test_search_fallback_tries_levels_before_resource() -> None:
    farm = FarmWorkflow()
    assert farm.advance_search_plan() is True
    assert farm._target() == ("iron", 6)
    assert farm.advance_search_plan() is True
    assert farm._target() == ("iron", 5)


def test_find_resource_keeps_the_planned_resource_and_level() -> None:
    farm = FarmWorkflow()
    farm.decide(FarmGameState.CITY)
    farm.decide(FarmGameState.WORLD_MAP, ready_teams=(2,))
    farm.decide(FarmGameState.RESOURCE_SEARCH)

    decision = farm.decide(FarmGameState.RESOURCE_SEARCH)

    assert decision.step == FarmStep.FIND_RESOURCE
    assert (decision.resource, decision.level, decision.team) == ("iron", 7, 2)


def test_team_selection_cannot_advance_to_dispatch_without_selection_evidence() -> None:
    farm = FarmWorkflow()
    farm.decide(FarmGameState.CITY)
    farm.decide(FarmGameState.WORLD_MAP, ready_teams=(2,))
    farm.decide(FarmGameState.RESOURCE_SEARCH)
    farm.decide(FarmGameState.RESOURCE_POPUP)
    farm.decide(FarmGameState.TEAM_SELECTION)

    decision = farm.decide(FarmGameState.TEAM_SELECTION)

    assert decision.step == FarmStep.SELECT_TEAM
    assert decision.input_allowed is False
