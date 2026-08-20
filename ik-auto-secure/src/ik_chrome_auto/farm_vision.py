"""Browser port of the ADB game-state detector's evidence rules.

The ADB implementation used screenshots from LDPlayer.  This module uses the
same template IDs and confirmation rules, but consumes evidence produced from a
Chrome canvas capture.  It deliberately contains no screen coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class FarmTemplateId(StrEnum):
    WORLD_MAP_ANCHOR = "world_map_anchor"
    WORLD_MAP_PIN_BUTTON = "world_map_pin_button"
    CONTINENT_MAP_TITLE = "continent_map_title"
    CONTINENT_MAP_HOME_TERRITORY_ANCHOR = "continent_map_home_territory_anchor"
    CONTINENT_MAP_PIN_BUTTON = "continent_map_pin_button"
    CONTINENT_MAP_SEARCH_TARGET_PIN = "continent_map_search_target_pin"
    RESOURCE_SEARCH_PANEL_ANCHOR = "resource_search_panel_anchor"
    SEARCH_BUTTON_ENABLED = "search_button_enabled"
    LEVEL_MINUS_BUTTON = "level_minus_button"
    RESOURCE_TAB_SELECTED = "resource_tab_selected"
    RESOURCE_TAB_UNSELECTED = "resource_tab_unselected"
    RESOURCE_POPUP_INFO_ANCHOR = "resource_popup_info_anchor"
    RESOURCE_POPUP_IRON_TITLE = "resource_popup_iron_title"
    GATHER_BUTTON_ENABLED = "gather_button_enabled"
    STORAGE_LIMIT_DIALOG_ANCHOR = "storage_limit_dialog_anchor"
    STORAGE_LIMIT_CANCEL_BUTTON = "storage_limit_cancel_button"
    RESOURCE_EXPIRY_DIALOG_ANCHOR = "resource_expiry_dialog_anchor"
    TEAM_SELECTION_PANEL_ANCHOR = "team_selection_panel_anchor"
    TEAM_ADJUST_FORMATION_BUTTON = "team_adjust_formation_button"
    TEAM_ACTION_BUTTON_ENABLED = "team_action_button_enabled"
    CITY_TO_WORLD_MAP_BUTTON = "city_to_world_map_button"
    BROWSER_CANVAS_READY_ANCHOR = "browser_canvas_ready_anchor"
    BROWSER_WORLD_MAP_BACK_BUTTON = "browser_world_map_back_button"
    BROWSER_WORLD_MAP_COORDINATE_PIN = "browser_world_map_coordinate_pin"
    BROWSER_CITY_CONTINENT_MAP_BUTTON = "browser_city_continent_map_button"
    BROWSER_RESOURCE_SEARCH_BUTTON = "browser_resource_search_button"
    BROWSER_RESOURCE_SEARCH_PANEL = "browser_resource_search_panel"
    BROWSER_RESOURCE_TAB_BUTTON = "browser_resource_tab_button"
    BROWSER_FOOD_RESOURCE_BUTTON = "browser_food_resource_button"
    BROWSER_WOOD_RESOURCE_BUTTON = "browser_wood_resource_button"
    BROWSER_STONE_RESOURCE_BUTTON = "browser_stone_resource_button"
    BROWSER_IRON_RESOURCE_BUTTON = "browser_iron_resource_button"
    # Each resource icon has a distinct selected treatment (gold outline and
    # orange label).  These are post-click evidence only: the workflow may
    # not submit a search until its chosen resource is visibly active.
    BROWSER_FOOD_RESOURCE_ACTIVE = "browser_food_resource_active"
    BROWSER_WOOD_RESOURCE_ACTIVE = "browser_wood_resource_active"
    BROWSER_STONE_RESOURCE_ACTIVE = "browser_stone_resource_active"
    BROWSER_IRON_RESOURCE_ACTIVE = "browser_iron_resource_active"
    BROWSER_SEARCH_TARGET_CHECKBOX_UNCHECKED = "browser_search_target_checkbox_unchecked"
    BROWSER_SEARCH_BUTTON_ENABLED = "browser_search_button_enabled"
    BROWSER_TOAST_NOT_FOUND = "browser_toast_not_found"
    BROWSER_TOAST_NOT_FOUND_SHORT = "browser_toast_not_found_short"
    BROWSER_TOAST_OTHER_REGION = "browser_toast_other_region"
    BROWSER_TOAST_LEVEL_TOO_LOW = "browser_toast_level_too_low"
    BROWSER_TARGET_RESOURCE_EXPIRY_TOAST = "browser_target_resource_expiry_toast"
    BROWSER_TARGET_RESOURCE_EXPIRY_CONFIRM = "browser_target_resource_expiry_confirm"
    BROWSER_GATHER_BUTTON_ENABLED = "browser_gather_button_enabled"
    BROWSER_TEAM_SELECTION_PANEL = "browser_team_selection_panel"
    BROWSER_TEAM_ACTION_BUTTON = "browser_team_action_button"
    BROWSER_TEAM_2_BADGE = "browser_team_2_badge"
    BROWSER_TEAM_3_BADGE = "browser_team_3_badge"
    BROWSER_TEAM_4_BADGE = "browser_team_4_badge"
    BROWSER_TEAM_SELECTED_BORDER = "browser_team_selected_border"


class DetectedGameState(StrEnum):
    UNKNOWN = "unknown"
    CITY = "city"
    WORLD_MAP = "world_map"
    CONTINENT_MAP = "continent_map"
    RESOURCE_SEARCH_PANEL = "resource_search_panel"
    RESOURCE_POPUP = "resource_popup"
    TEAM_SELECTION = "team_selection"
    STORAGE_LIMIT_DIALOG = "storage_limit_dialog"
    RESOURCE_EXPIRY_DIALOG = "resource_expiry_dialog"


class TeamRowState(StrEnum):
    """Scheduler-facing roster status, aligned with the ADB roster scan."""

    READY = "ready"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class TemplateEvidence:
    template_id: FarmTemplateId
    found: bool
    confidence: float = 0.0
    bounds: tuple[int, int, int, int] | None = None

    @property
    def actionable(self) -> bool:
        return self.found and self.bounds is not None and self.bounds[2] > 0 and self.bounds[3] > 0


@dataclass(frozen=True, slots=True)
class TeamRosterRow:
    """One verified/inferred World Map team row.

    A fresh Ready label proves its own numbered row exists.  As in the ADB
    implementation, all preceding rows are then known to be unlocked; one of
    those without a Ready label is Busy rather than silently treated ready.
    """

    team: int
    state: TeamRowState
    evidence: str


@dataclass(frozen=True, slots=True)
class GameDetectionResult:
    state: DetectedGameState
    evidence: tuple[TemplateEvidence, ...]
    successful: bool = True
    error: str | None = None
    ready_teams: tuple[int, ...] = ()
    team_roster: tuple[TeamRosterRow, ...] = ()

    def evidence_for(self, template_id: FarmTemplateId) -> TemplateEvidence:
        return next((item for item in self.evidence if item.template_id == template_id), TemplateEvidence(template_id, False))


DETECTION_TEMPLATES: tuple[FarmTemplateId, ...] = tuple(FarmTemplateId)


class BrowserGameStateDetector:
    """Classifies Chrome-canvas template evidence using the ADB detector rules."""

    def detect(self, evidence: Mapping[FarmTemplateId, TemplateEvidence]) -> GameDetectionResult:
        values = tuple(evidence.get(template_id, TemplateEvidence(template_id, False)) for template_id in DETECTION_TEMPLATES)
        found = {item.template_id: item.found for item in values}
        panel_chrome = any(found[item] for item in (
            FarmTemplateId.RESOURCE_SEARCH_PANEL_ANCHOR,
            FarmTemplateId.LEVEL_MINUS_BUTTON,
            FarmTemplateId.RESOURCE_TAB_SELECTED,
            FarmTemplateId.RESOURCE_TAB_UNSELECTED,
            FarmTemplateId.BROWSER_RESOURCE_SEARCH_PANEL,
        ))
        panel_confirmed = found[FarmTemplateId.BROWSER_RESOURCE_SEARCH_PANEL] or (
            panel_chrome and found[FarmTemplateId.SEARCH_BUTTON_ENABLED]
        )
        popup_signals = sum(found[item] for item in (
            FarmTemplateId.RESOURCE_POPUP_INFO_ANCHOR,
            FarmTemplateId.RESOURCE_POPUP_IRON_TITLE,
            FarmTemplateId.GATHER_BUTTON_ENABLED,
        ))
        popup_confirmed = popup_signals >= 2 and (
            found[FarmTemplateId.RESOURCE_POPUP_INFO_ANCHOR]
            or found[FarmTemplateId.RESOURCE_POPUP_IRON_TITLE]
        )
        # The web canvas uses a different resource-details popup from the ADB
        # client.  Its enabled "Thu thập" button is a specific, actionable
        # confirmation that a resource has been found, even when the legacy
        # information/title templates are not present.
        browser_popup_confirmed = found[FarmTemplateId.BROWSER_GATHER_BUTTON_ENABLED]
        team_confirmed = (
            found[FarmTemplateId.TEAM_SELECTION_PANEL_ANCHOR]
            and (
                found[FarmTemplateId.TEAM_ADJUST_FORMATION_BUTTON]
                or found[FarmTemplateId.TEAM_ACTION_BUTTON_ENABLED]
            )
        ) or (
            found[FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL]
            and found[FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON]
        )
        storage_confirmed = found[FarmTemplateId.STORAGE_LIMIT_DIALOG_ANCHOR] and found[FarmTemplateId.STORAGE_LIMIT_CANCEL_BUTTON]
        expiry_confirmed = found[FarmTemplateId.RESOURCE_EXPIRY_DIALOG_ANCHOR] and found[FarmTemplateId.STORAGE_LIMIT_CANCEL_BUTTON]
        continent_confirmed = any(found[item] for item in (
            FarmTemplateId.CONTINENT_MAP_TITLE,
            FarmTemplateId.CONTINENT_MAP_HOME_TERRITORY_ANCHOR,
            FarmTemplateId.CONTINENT_MAP_PIN_BUTTON,
        ))
        city_confirmed = (
            found[FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON]
            and not team_confirmed and not panel_confirmed and not popup_confirmed
            and not continent_confirmed
            and not found[FarmTemplateId.WORLD_MAP_ANCHOR]
            # The compact X/Y coordinate HUD is only present on World Map in
            # the browser build. It remains visible after a dispatch, when the
            # City-toggle artwork can otherwise match the home button.
            and not found[FarmTemplateId.BROWSER_WORLD_MAP_COORDINATE_PIN]
            and not found[FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON]
        )
        state = (
            DetectedGameState.RESOURCE_EXPIRY_DIALOG if expiry_confirmed else
            DetectedGameState.STORAGE_LIMIT_DIALOG if storage_confirmed else
            DetectedGameState.TEAM_SELECTION if team_confirmed else
            DetectedGameState.RESOURCE_SEARCH_PANEL if panel_confirmed else
            DetectedGameState.RESOURCE_POPUP if popup_confirmed or browser_popup_confirmed else
            DetectedGameState.CONTINENT_MAP if continent_confirmed else
            DetectedGameState.WORLD_MAP if (
                found[FarmTemplateId.WORLD_MAP_ANCHOR]
                or found[FarmTemplateId.BROWSER_WORLD_MAP_BACK_BUTTON]
                or found[FarmTemplateId.BROWSER_WORLD_MAP_COORDINATE_PIN]
                # On some web skins the stable World Map evidence is the
                # lower-left magnifier itself. It is a distinct World Map HUD
                # control, not the City toggle. Higher-priority panel/popup
                # states above prevent it from reclassifying their screens.
                or found[FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON]
            ) else
            DetectedGameState.CITY if city_confirmed else DetectedGameState.UNKNOWN
        )
        return GameDetectionResult(state, values)
