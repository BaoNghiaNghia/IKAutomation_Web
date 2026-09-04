from __future__ import annotations

from types import SimpleNamespace

from ik_chrome_auto.runner import ProfileWorker
from ik_chrome_auto.farm_vision import TeamRosterRow, TeamRowState
from ik_chrome_auto.farm_workflow import FarmGameState


def evidence(bounds: tuple[int, int, int, int]):
    return SimpleNamespace(actionable=True, bounds=bounds)


def test_team_one_row_is_inferred_from_numbered_rows_below_it() -> None:
    badges = {
        2: evidence((4, 100, 24, 24)),
        3: evidence((4, 191, 24, 24)),
        4: evidence((4, 277, 24, 24)),
    }

    row = ProfileWorker._team_row_for_selection(1, badges, (835, 432))

    assert row == (0, 1, 144, 82)


def test_team_one_selected_border_is_verified_against_inferred_row() -> None:
    badges = {
        2: evidence((4, 100, 24, 24)),
        3: evidence((4, 191, 24, 24)),
    }
    row = ProfileWorker._team_row_for_selection(1, badges, (835, 432))
    selected_border = evidence((1, 10, 7, 67))

    assert ProfileWorker._is_expected_team_selected(None, selected_border, row) is True


def test_numbered_team_uses_its_fixed_panel_row() -> None:
    badges = {3: evidence((4, 191, 24, 24))}

    row = ProfileWorker._team_row_for_selection(3, badges, (835, 432))

    assert row == (0, 179, 144, 82)


def test_false_team_two_badge_match_cannot_redirect_click_to_team_three() -> None:
    # Production log: Team 2 falsely matched the Team 3 badge at y=188.
    badges = {2: evidence((4, 188, 24, 24))}

    row = ProfileWorker._team_row_for_selection(2, badges, (835, 432))

    assert row == (0, 90, 144, 82)


def test_dispatch_accepts_world_map_when_selected_team_became_busy() -> None:
    roster = (
        TeamRosterRow(1, TeamRowState.BUSY, "BusyLabel"),
        TeamRosterRow(2, TeamRowState.READY, "ReadyLabel"),
    )

    assert ProfileWorker._is_dispatch_postcondition_verified(
        state=FarmGameState.WORLD_MAP,
        team_panel_visible=False,
        team_action_visible=False,
        world_map_anchor_visible=True,
        expected_team=1,
        roster=roster,
    ) is True


def test_dispatch_rejects_world_map_when_selected_team_is_still_ready() -> None:
    roster = (TeamRosterRow(1, TeamRowState.READY, "ReadyLabel"),)

    assert ProfileWorker._is_dispatch_postcondition_verified(
        state=FarmGameState.WORLD_MAP,
        team_panel_visible=False,
        team_action_visible=False,
        world_map_anchor_visible=True,
        expected_team=1,
        roster=roster,
    ) is False


def test_dispatch_rejects_visible_team_panel() -> None:
    assert ProfileWorker._is_dispatch_postcondition_verified(
        state=FarmGameState.WORLD_MAP,
        team_panel_visible=True,
        team_action_visible=True,
        world_map_anchor_visible=True,
        expected_team=1,
        roster=(),
    ) is False


def test_readable_roster_replaces_stale_ready_team_state() -> None:
    roster = (
        TeamRosterRow(1, TeamRowState.BUSY, "BusyLabel"),
        TeamRosterRow(2, TeamRowState.BUSY, "BusyLabel"),
        TeamRosterRow(3, TeamRowState.READY, "ReadyLabel"),
    )

    assert ProfileWorker._ready_teams_from_roster(roster) == (3,)


def test_all_busy_roster_produces_no_ready_team_for_post_dispatch_scan() -> None:
    roster = (
        TeamRosterRow(1, TeamRowState.BUSY, "BusyLabel"),
        TeamRosterRow(2, TeamRowState.BUSY, "BusyLabel"),
        TeamRosterRow(3, TeamRowState.BUSY, "BusyLabel"),
    )

    assert ProfileWorker._ready_teams_from_roster(roster) == ()


def test_selected_team_turning_busy_recovers_an_implicit_dispatch() -> None:
    roster = (
        TeamRosterRow(1, TeamRowState.BUSY, "BusyLabel"),
        TeamRosterRow(2, TeamRowState.READY, "ReadyLabel"),
    )

    assert ProfileWorker._selected_team_became_busy(1, roster) is True
    assert ProfileWorker._selected_team_became_busy(2, roster) is False
