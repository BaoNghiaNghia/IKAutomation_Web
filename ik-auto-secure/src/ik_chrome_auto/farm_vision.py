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
    BROWSER_RESOURCE_SEARCH_BUTTON = "browser_resource_search_button"
    BROWSER_RESOURCE_SEARCH_PANEL = "browser_resource_search_panel"


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
class GameDetectionResult:
    state: DetectedGameState
    evidence: tuple[TemplateEvidence, ...]
    successful: bool = True
    error: str | None = None
    ready_teams: tuple[int, ...] = ()

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
        team_confirmed = found[FarmTemplateId.TEAM_SELECTION_PANEL_ANCHOR] and (
            found[FarmTemplateId.TEAM_ADJUST_FORMATION_BUTTON]
            or found[FarmTemplateId.TEAM_ACTION_BUTTON_ENABLED]
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
            and not continent_confirmed and not found[FarmTemplateId.WORLD_MAP_ANCHOR]
        )
        state = (
            DetectedGameState.RESOURCE_EXPIRY_DIALOG if expiry_confirmed else
            DetectedGameState.STORAGE_LIMIT_DIALOG if storage_confirmed else
            DetectedGameState.TEAM_SELECTION if team_confirmed else
            DetectedGameState.RESOURCE_SEARCH_PANEL if panel_confirmed else
            DetectedGameState.RESOURCE_POPUP if popup_confirmed else
            DetectedGameState.CONTINENT_MAP if continent_confirmed else
            DetectedGameState.WORLD_MAP if found[FarmTemplateId.WORLD_MAP_ANCHOR] else
            DetectedGameState.CITY if city_confirmed else DetectedGameState.UNKNOWN
        )
        return GameDetectionResult(state, values)
