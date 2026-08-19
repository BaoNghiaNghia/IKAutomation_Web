from __future__ import annotations

from ik_chrome_auto.farm_vision import (
    BrowserGameStateDetector,
    DetectedGameState,
    FarmTemplateId,
    GameDetectionResult,
    TeamRosterRow,
    TeamRowState,
    TemplateEvidence,
)


def detect(*templates: FarmTemplateId):
    return BrowserGameStateDetector().detect({item: TemplateEvidence(item, True, 0.95, (10, 20, 30, 40)) for item in templates})


def test_city_requires_map_button_and_no_higher_priority_overlay() -> None:
    assert detect(FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON).state == DetectedGameState.CITY
    assert detect(FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON, FarmTemplateId.WORLD_MAP_ANCHOR).state == DetectedGameState.WORLD_MAP
    assert detect(FarmTemplateId.BROWSER_CANVAS_READY_ANCHOR).state == DetectedGameState.UNKNOWN
    assert detect(FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON).state == DetectedGameState.UNKNOWN
    assert detect(FarmTemplateId.BROWSER_RESOURCE_SEARCH_PANEL).state == DetectedGameState.RESOURCE_SEARCH_PANEL
    assert detect(FarmTemplateId.BROWSER_RESOURCE_TAB_BUTTON).state == DetectedGameState.UNKNOWN
    assert detect(FarmTemplateId.BROWSER_IRON_RESOURCE_BUTTON).state == DetectedGameState.UNKNOWN


def test_detection_result_keeps_ready_team_slots() -> None:
    roster = (
        TeamRosterRow(1, TeamRowState.READY, "ReadyLabel"),
        TeamRosterRow(2, TeamRowState.BUSY, "InferredPrecedingRow"),
        TeamRosterRow(3, TeamRowState.READY, "ReadyLabel"),
    )
    result = GameDetectionResult(DetectedGameState.WORLD_MAP, (), ready_teams=(1, 3), team_roster=roster)
    assert result.ready_teams == (1, 3)
    assert [(row.team, row.state.value) for row in result.team_roster] == [
        (1, "ready"), (2, "busy"), (3, "ready"),
    ]


def test_panel_requires_two_independent_signals() -> None:
    assert detect(FarmTemplateId.RESOURCE_SEARCH_PANEL_ANCHOR).state == DetectedGameState.UNKNOWN
    assert detect(FarmTemplateId.RESOURCE_SEARCH_PANEL_ANCHOR, FarmTemplateId.SEARCH_BUTTON_ENABLED).state == DetectedGameState.RESOURCE_SEARCH_PANEL


def test_dialog_priority_matches_adb_detector() -> None:
    result = detect(
        FarmTemplateId.TEAM_SELECTION_PANEL_ANCHOR,
        FarmTemplateId.TEAM_ACTION_BUTTON_ENABLED,
        FarmTemplateId.STORAGE_LIMIT_DIALOG_ANCHOR,
        FarmTemplateId.STORAGE_LIMIT_CANCEL_BUTTON,
        FarmTemplateId.RESOURCE_EXPIRY_DIALOG_ANCHOR,
    )
    assert result.state == DetectedGameState.RESOURCE_EXPIRY_DIALOG


def test_popup_requires_multiple_signals() -> None:
    assert detect(FarmTemplateId.GATHER_BUTTON_ENABLED).state == DetectedGameState.UNKNOWN
    assert detect(FarmTemplateId.RESOURCE_POPUP_INFO_ANCHOR, FarmTemplateId.GATHER_BUTTON_ENABLED).state == DetectedGameState.RESOURCE_POPUP


def test_browser_team_selection_requires_panel_and_action() -> None:
    assert detect(FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL).state == DetectedGameState.UNKNOWN
    assert detect(
        FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL,
        FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON,
    ).state == DetectedGameState.TEAM_SELECTION
