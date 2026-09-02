from __future__ import annotations

from ik_chrome_auto.farm_workflow import FarmGameState, FarmStep, FarmWorkflow


def test_unknown_state_never_allows_input() -> None:
    decision = FarmWorkflow().decide(FarmGameState.UNKNOWN, target_verified=True)
    assert decision.step == FarmStep.PREFLIGHT
    assert decision.input_allowed is False


def test_temporary_unknown_frame_keeps_the_verified_farm_stage() -> None:
    farm = FarmWorkflow()
    farm.decide(FarmGameState.CITY)
    farm.decide(FarmGameState.WORLD_MAP, ready_teams=(3, 4))

    decision = farm.decide(FarmGameState.UNKNOWN)

    assert decision.step == FarmStep.OPEN_SEARCH
    assert decision.input_allowed is False
    assert farm.step == FarmStep.OPEN_SEARCH


def test_new_cycle_entering_on_world_map_must_verify_city_first() -> None:
    farm = FarmWorkflow()

    assert farm.decide(FarmGameState.WORLD_MAP).step == FarmStep.RETURN_TO_CITY
    assert farm.decide(FarmGameState.WORLD_MAP).step == FarmStep.RETURN_TO_CITY
    assert farm.decide(FarmGameState.CITY).step == FarmStep.ENTER_WORLD_MAP


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


def test_search_fallback_rotates_resources_before_area_round() -> None:
    farm = FarmWorkflow(resource_order=("iron", "stone", "wood", "food"))
    assert farm.advance_search_plan() is True
    assert farm._target() == ("stone", 7)
    assert farm.advance_search_plan() is True
    assert farm._target() == ("wood", 7)
    assert farm.advance_search_plan() is True
    assert farm._target() == ("food", 7)
    assert farm.advance_search_plan() is False
    assert farm._target() == ("iron", 7)


def test_find_resource_keeps_the_planned_resource_and_level() -> None:
    farm = FarmWorkflow(resource_order=("iron", "stone", "wood", "food"))
    farm.decide(FarmGameState.CITY)
    farm.decide(FarmGameState.WORLD_MAP, ready_teams=(2,))
    farm.decide(FarmGameState.RESOURCE_SEARCH)

    decision = farm.decide(FarmGameState.RESOURCE_SEARCH)

    assert decision.step == FarmStep.FIND_RESOURCE
    assert (decision.resource, decision.level, decision.team) == ("iron", 7, 2)


def test_delayed_resource_popup_recovers_after_search_plan_was_provisionally_rotated() -> None:
    farm = FarmWorkflow(resource_order=("iron", "stone", "wood", "food"))
    farm.decide(FarmGameState.CITY)
    farm.decide(FarmGameState.WORLD_MAP, ready_teams=(3,))
    # A timeout after Search can temporarily return the workflow to this
    # stage. A real popup arriving late must still authorise Gather.
    farm.step = FarmStep.OPEN_SEARCH

    decision = farm.decide(FarmGameState.RESOURCE_POPUP, target_verified=True)

    assert decision.step == FarmStep.OPEN_TEAM_SELECTION
    assert decision.team == 3
    assert decision.input_allowed is True


def test_resource_plan_is_randomized_once_then_advances_level_after_area_pool() -> None:
    farm = FarmWorkflow(resource_order=("wood", "food", "stone", "iron"))

    assert farm._target() == ("wood", 7)
    assert farm.advance_search_plan() is True
    assert farm._target() == ("food", 7)
    assert farm.advance_search_plan() is True
    assert farm._target() == ("stone", 7)
    assert farm.advance_search_plan() is True
    assert farm._target() == ("iron", 7)
    assert farm.advance_search_plan() is False
    assert farm.advance_level_plan() is False


def test_visible_resource_level_overrides_the_fallback_without_clicking_controls() -> None:
    farm = FarmWorkflow(resource_order=("iron", "stone", "wood", "food"))

    farm.set_observed_level(6)

    assert farm.current_target() == ("iron", 6)


def test_first_ready_team_from_initial_roster_is_locked_for_the_cycle() -> None:
    farm = FarmWorkflow()
    farm.decide(FarmGameState.CITY)

    decision = farm.decide(FarmGameState.WORLD_MAP, ready_teams=(1, 3, 4))

    assert decision.team == 1
    assert decision.step == FarmStep.OPEN_SEARCH


def test_waiting_for_ready_team_resumes_from_fresh_world_map_roster() -> None:
    farm = FarmWorkflow()
    farm.decide(FarmGameState.CITY)

    waiting = farm.decide(FarmGameState.WORLD_MAP, ready_teams=())
    resumed = farm.decide(FarmGameState.WORLD_MAP, ready_teams=(3, 4))

    assert waiting.step == FarmStep.WAITING
    assert resumed.step == FarmStep.OPEN_SEARCH
    assert resumed.team == 3
    assert farm.team == 3


def test_post_dispatch_wait_does_not_restart_from_stale_ready_roster() -> None:
    farm = FarmWorkflow()
    farm.decide(FarmGameState.CITY)
    farm.decide(FarmGameState.WORLD_MAP, ready_teams=(2,))
    farm.decide(FarmGameState.RESOURCE_SEARCH)
    farm.decide(FarmGameState.RESOURCE_POPUP)
    farm.decide(FarmGameState.TEAM_SELECTION)
    farm.decide(FarmGameState.TEAM_SELECTION, team_selected=True)
    farm.decide(FarmGameState.TEAM_SELECTION, dispatch_verified=True)

    decision = farm.decide(FarmGameState.WORLD_MAP, ready_teams=(1, 3, 4))

    assert decision.step == FarmStep.WAITING
    assert farm.team == 2


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
