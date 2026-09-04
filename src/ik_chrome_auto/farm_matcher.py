"""OpenCV template matching over a captured browser game canvas."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ik_chrome_auto.farm_vision import (
    BrowserGameStateDetector,
    DETECTION_TEMPLATES,
    DetectedGameState,
    FarmTemplateId,
    GameDetectionResult,
    TeamRosterRow,
    TeamRowState,
    TemplateEvidence,
)

# The source pack was captured at 1280x720.  A browser canvas is frequently
# stretched independently on each axis by its host page, so scaling from only
# its width makes templates noticeably too tall (or too short) and prevents a
# valid match.  Keep both dimensions as the template's reference frame.
_REFERENCE_WIDTH = 1280
_REFERENCE_HEIGHT = 720
_BROWSER_REFERENCE_WIDTH = 836
_BROWSER_REFERENCE_HEIGHT = 433
_READY_TEAM_LABEL = "browser_ready_team_label.png"
_READY_TEAM_LABEL_SNOW = "browser_ready_team_label_snow.png"
_BUSY_TEAM_LABEL = "browser_busy_team_label.png"
# The countdown digits after the busy icon change every second.  Matching the
# entire label made a busy row intermittently score below a false Ready match.
_BUSY_TEAM_INDICATOR_WIDTH = 26
# Grayscale correlation alone is unsafe for the tiny Ready label: changing
# countdown/resource text in a busy row can share the same broad light/dark
# pattern.  Its glyph edges must also match before a row becomes schedulable.
# The live false-positive reported on account 2 scored 0.2205 here, while its
# two genuine Ready rows scored 0.2610 and 0.2584.
_READY_TEAM_MIN_EDGE_SCORE = 0.18
_READY_TEAM_MIN_GRAYSCALE_SCORE = 0.76
# A changing countdown can weaken the compact Busy prefix, but the supplied
# false-ready rows remain above 0.52 while genuine Ready rows stay below 0.20.
# Reject Busy first so a snowy Ready label with slightly softer glyph edges
# can be admitted without making an active march schedulable.
_BUSY_TEAM_MIN_SCORE = 0.52
# Browser screenshots have their own HUD scaling. These are stable roster
# layout proportions, not gameplay click coordinates. A matched Ready label
# is mapped to its actual row before any scheduler decision is made.
_ROSTER_TOP_RATIO = 0.425
_ROSTER_ROW_HEIGHT_RATIO = 0.071
_ROSTER_BOTTOM_RATIO = 0.720
_ROSTER_LABEL_LEFT_RATIO = 0.048
_ROSTER_LABEL_RIGHT_RATIO = 0.145
_ROSTER_PORTRAIT_LEFT_RATIO = 0.014
_ROSTER_PORTRAIT_RIGHT_RATIO = 0.055
_ROSTER_PORTRAIT_MIN_EDGE_DENSITY = 0.155


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    filename: str
    threshold: float = 0.84
    region: str = "all"
    reference_width: int = _REFERENCE_WIDTH
    reference_height: int = _REFERENCE_HEIGHT
    alternatives: tuple[str, ...] = ()
    uniform_width_scale: bool = False
    # Text rendered in the canvas can differ by one or two device pixels
    # between Chrome profiles.  Text-specific templates may opt into a small
    # bounded scale sweep and luminance matching.
    scale_variants: tuple[float, ...] = (1.0,)
    grayscale: bool = False
    # Edge matching is used for controls whose artwork changes colour with
    # the game environment, while their silhouette remains stable.
    edge: bool = False


SPECS: dict[FarmTemplateId, TemplateSpec] = {
    FarmTemplateId.WORLD_MAP_ANCHOR: TemplateSpec(
        "world_map_anchor.png",
        # The green World Map skin in the latest captured profile scores
        # 0.72 against this anchor, while the City screenshot is below 0.45.
        # 0.70 therefore admits the verified map state without allowing the
        # City control to be confused for a World Map transition.
        threshold=0.70,
        region="lower_left",
    ),
    # In City, the bottom-left parchment/compass control opens World Map.
    # Use the full control captured from the real 1280x720 renderer instead
    # of the legacy browser crop, then scale it with the current canvas.
    FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON: TemplateSpec(
        "browser_open_world_map_1280.png",
        threshold=0.70,
        # The Map control is left of the Mail button.  Keep its region narrow
        # enough that the neighbouring envelope cannot become a false map
        # match on a 1280×720 City canvas.
        region="map_corner",
        reference_width=1280,
        reference_height=720,
        scale_variants=(0.94, 0.97, 1.0, 1.03, 1.06),
        edge=True,
    ),
    # In World Map, the bottom-left castle control returns to City. These two
    # full controls were captured from the real 1280x720 renderer. The earlier
    # tight browser crop omitted too much of the castle and missed the red
    # skin; match the icon shape so terrain colour cannot affect detection.
    FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON: TemplateSpec(
        "browser_city_return_green_1280.png",
        threshold=0.68,
        # Exclude the adjacent envelope completely. Matching only authorises
        # the action; runner.py taps the fixed canvas-relative toggle point.
        region="map_corner",
        reference_width=1280,
        reference_height=720,
        alternatives=(
            "browser_city_return_red_1280.png",
        ),
        scale_variants=(0.94, 0.97, 1.0, 1.03, 1.06),
        edge=True,
    ),
    FarmTemplateId.CONTINENT_MAP_TITLE: TemplateSpec("continent_map_title.png"),
    FarmTemplateId.CONTINENT_MAP_HOME_TERRITORY_ANCHOR: TemplateSpec("continent_map_home_territory_anchor.png", region="center"),
    FarmTemplateId.CONTINENT_MAP_PIN_BUTTON: TemplateSpec("continent_map_pin_button.png", region="top_left"),
    FarmTemplateId.CONTINENT_MAP_SEARCH_TARGET_PIN: TemplateSpec(
        "continent_map_search_target_pin.png", reference_width=1280, reference_height=720
    ),
    FarmTemplateId.WORLD_MAP_PIN_BUTTON: TemplateSpec(
        "world_map_pin_button.png", region="top_left", reference_width=1280, reference_height=720
    ),
    FarmTemplateId.RESOURCE_SEARCH_PANEL_ANCHOR: TemplateSpec("resource_search_panel_anchor.png", region="lower"),
    FarmTemplateId.SEARCH_BUTTON_ENABLED: TemplateSpec("search_button_enabled.png", region="lower"),
    FarmTemplateId.LEVEL_MINUS_BUTTON: TemplateSpec("level_minus_button.png", region="lower"),
    FarmTemplateId.RESOURCE_TAB_SELECTED: TemplateSpec("resource_tab_selected.png", region="lower"),
    FarmTemplateId.RESOURCE_TAB_UNSELECTED: TemplateSpec("resource_tab_unselected.png", region="lower"),
    FarmTemplateId.RESOURCE_POPUP_INFO_ANCHOR: TemplateSpec("resource_popup_info_anchor.png"),
    FarmTemplateId.RESOURCE_POPUP_IRON_TITLE: TemplateSpec("resource_popup_iron_title.png"),
    FarmTemplateId.GATHER_BUTTON_ENABLED: TemplateSpec("gather_button_enabled.png"),
    FarmTemplateId.STORAGE_LIMIT_DIALOG_ANCHOR: TemplateSpec("storage_limit_dialog_anchor.png"),
    FarmTemplateId.STORAGE_LIMIT_CANCEL_BUTTON: TemplateSpec("storage_limit_cancel_button.png"),
    FarmTemplateId.RESOURCE_EXPIRY_DIALOG_ANCHOR: TemplateSpec("resource_expiry_dialog_anchor.png"),
    FarmTemplateId.TEAM_SELECTION_PANEL_ANCHOR: TemplateSpec("team_selection_panel_anchor.png"),
    FarmTemplateId.TEAM_ADJUST_FORMATION_BUTTON: TemplateSpec("team_adjust_formation_button.png"),
    FarmTemplateId.TEAM_ACTION_BUTTON_ENABLED: TemplateSpec("team_action_button_enabled.png"),
    # The publisher mark remains visible once the browser's World Map canvas
    # has finished loading. It is only used with the post-click timing guard
    # in ProfileWorker; alone it never authorises input.
    FarmTemplateId.BROWSER_CANVAS_READY_ANCHOR: TemplateSpec(
        "browser_canvas_ready_anchor.png",
        threshold=0.80,
        region="right",
        reference_width=835,
        reference_height=432,
    ),
    # The blue return control in the top-left exists only while the portal is
    # on World Map. It prevents the City icon at lower-left from being used as
    # a false City classification when a new farm cycle begins.
    FarmTemplateId.BROWSER_WORLD_MAP_BACK_BUTTON: TemplateSpec(
        "browser_world_map_back_button.png",
        threshold=0.80,
        region="top_left",
        reference_width=838,
        reference_height=436,
    ),
    # The web portal opens Continent Map from the small location-pin control
    # next to the live X/Y indicator, unlike the larger ADB minimap button.
    FarmTemplateId.BROWSER_WORLD_MAP_COORDINATE_PIN: TemplateSpec(
        "browser_world_map_coordinate_pin.png",
        # Current 1280x720 World Map skins score 0.740-0.746, while the
        # supplied City frame is 0.716. This bounded threshold recognises the
        # live Map HUD without allowing City to authorise a Search click.
        threshold=0.735,
        region="top",
        reference_width=835,
        reference_height=432,
        scale_variants=(0.94, 0.97, 1.0, 1.03, 1.06),
        grayscale=True,
    ),
    FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON: TemplateSpec(
        "browser_city_continent_map_button.png",
        threshold=0.80,
        region="lower_left",
        # Current World Map HUD capture supplied from the live 1280x720 profile.
        # The old 28x25 crop came from an 850x437 frame and scaled into a
        # different aspect, so the verified map view could never authorise opening
        # Continent Map.
        reference_width=1280,
        reference_height=720,
        alternatives=("browser_city_continent_map_button_legacy_1280.png",),
        scale_variants=(0.94, 0.97, 1.0, 1.03, 1.06),
    ),
    FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON: TemplateSpec(
        # Supplied cropped magnifier from the live 1280×720 World Map HUD.
        # It must be scaled in that canvas reference frame; the old compact
        # 836×433 reference enlarged this icon and could miss it or confuse a
        # nearby lower-left control.
        "browser_resource_search_button.png",
        threshold=0.76,
        region="lower_left",
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_RESOURCE_SEARCH_PANEL: TemplateSpec(
        "browser_resource_search_panel_anchor.png",
        threshold=0.80,
        region="lower",
        reference_width=835,
        reference_height=432,
    ),
    FarmTemplateId.BROWSER_RESOURCE_TAB_BUTTON: TemplateSpec(
        "browser_resource_tab_unselected.png",
        threshold=0.78,
        region="lower_left",
        reference_width=835,
        reference_height=432,
    ),
    FarmTemplateId.BROWSER_FOOD_RESOURCE_BUTTON: TemplateSpec(
        "browser_resource_food.png", threshold=0.80, region="lower",
        reference_width=881, reference_height=239, uniform_width_scale=True,
    ),
    FarmTemplateId.BROWSER_WOOD_RESOURCE_BUTTON: TemplateSpec(
        # Browser canvas capture of the selected/unselected Wood artwork is
        # consistently softer than the original source pack. Production
        # captures score ~0.687 while the panel and Search button are already
        # independently verified, so 0.66 is the safe observed gate here.
        "browser_resource_wood.png", threshold=0.66, region="lower",
        reference_width=881, reference_height=239, uniform_width_scale=True,
    ),
    FarmTemplateId.BROWSER_STONE_RESOURCE_BUTTON: TemplateSpec(
        "browser_resource_stone.png", threshold=0.80, region="lower",
        reference_width=881, reference_height=239, uniform_width_scale=True,
    ),
    FarmTemplateId.BROWSER_IRON_RESOURCE_BUTTON: TemplateSpec(
        "browser_resource_iron_unselected.png",
        threshold=0.78,
        region="lower",
        reference_width=835,
        reference_height=432,
    ),
    # Captured from the four active states supplied for the web UI.  Match
    # the complete icon plus its orange label so an adjacent inactive icon is
    # never mistaken for the selected resource.  Use width-only scaling: this
    # lower panel keeps its native artwork aspect while the canvas height may
    # be letterboxed by the host page.
    FarmTemplateId.BROWSER_FOOD_RESOURCE_ACTIVE: TemplateSpec(
        # Captured from the supplied 1280x720 renderer, including the gold
        # active ring and orange resource label. This prevents the inactive
        # neighbouring icons from being accepted as the selected resource.
        "browser_resource_food_active_1280.png", threshold=0.72, region="lower",
        reference_width=1280, reference_height=720,
    ),
    FarmTemplateId.BROWSER_WOOD_RESOURCE_ACTIVE: TemplateSpec(
        "browser_resource_wood_active_1280.png", threshold=0.72, region="lower",
        reference_width=1280, reference_height=720,
    ),
    FarmTemplateId.BROWSER_STONE_RESOURCE_ACTIVE: TemplateSpec(
        "browser_resource_stone_active_1280.png", threshold=0.72, region="lower",
        reference_width=1280, reference_height=720,
    ),
    FarmTemplateId.BROWSER_IRON_RESOURCE_ACTIVE: TemplateSpec(
        "browser_resource_iron_active_1280.png", threshold=0.72, region="lower",
        reference_width=1280, reference_height=720,
    ),
    # The visible ``Cấp: n / n`` readout is evidence only.  The runner never
    # clicks + or −; it uses this value as the current search level.
    FarmTemplateId.BROWSER_RESOURCE_LEVEL_6: TemplateSpec(
        "browser_resource_level_6.png", threshold=0.82, region="resource_level",
        reference_width=1280, reference_height=720,
        scale_variants=(0.97, 1.0, 1.03), grayscale=True,
    ),
    FarmTemplateId.BROWSER_RESOURCE_LEVEL_7: TemplateSpec(
        "browser_resource_level_7.png", threshold=0.82, region="resource_level",
        reference_width=1280, reference_height=720,
        scale_variants=(0.97, 1.0, 1.03), grayscale=True,
    ),
    # The resource panel's "only search eligible target" checkbox must be
    # enabled before Search.  Match the *unchecked* glyph only; after tapping
    # it, its disappearance is the verified checked state.  This keeps the
    # input anchored to the live UI rather than a hard-coded canvas point.
    FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_UNCHECKED: TemplateSpec(
        "browser_search_target_checkbox_unchecked.png",
        # Green World Map skin uses a smaller blue unchecked circle. The
        # original template scored only ~0.53 there, so include its verified
        # crop rather than treating a failed match as an already-checked box.
        # The supplied live canvas crop scores ~0.52 against the compact
        # portal skin. This template is only used in the verified resource
        # panel's tight lower-right region and must disappear after the tap,
        # so 0.50 remains a bounded, safe gate.
        threshold=0.50,
        region="search_checkbox",
        reference_width=836,
        reference_height=433,
        alternatives=("browser_search_target_checkbox_unchecked_green.png",),
        scale_variants=(0.94, 0.97, 1.0, 1.03, 1.06),
    ),
    FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_CHECKED: TemplateSpec(
        # Supplied live screenshot of the blue checkmark state. It is used as
        # the positive post-click condition before Search is allowed.  Live
        # 1280x720 profiles score about 0.74 after Chrome's text/subpixel
        # rendering changes, so keep a small margin below that observed
        # result while retaining the tight checkbox-only region.
        "browser_search_target_checkbox_checked.png",
        threshold=0.70,
        region="search_checkbox",
        reference_width=1014,
        reference_height=275,
        uniform_width_scale=True,
    ),
    FarmTemplateId.BROWSER_SEARCH_BUTTON_ENABLED: TemplateSpec(
        "browser_search_button_enabled.png",
        threshold=0.80,
        region="lower",
        # Replaced with the supplied live Search-button capture. The newest
        # production diagnostic shows this crop at its native 1280×720 size;
        # the earlier 1014-wide assumption enlarged it by 26% and made the
        # button miss at 0.50 confidence instead of its observed 0.995.
        reference_width=1280,
        reference_height=720,
    ),
    # These localized toast anchors are shared from the ADB pack. They are
    # observed after a Search tap only; they never authorise an input.
    FarmTemplateId.BROWSER_TOAST_NOT_FOUND: TemplateSpec(
        "browser_toast_not_found.png", threshold=0.78, region="toast"
    ),
    FarmTemplateId.BROWSER_TOAST_NOT_FOUND_SHORT: TemplateSpec(
        "browser_toast_not_found_short.png", threshold=0.78, region="toast"
    ),
    FarmTemplateId.BROWSER_TOAST_OTHER_REGION: TemplateSpec(
        "browser_toast_other_region.png", threshold=0.78, region="toast"
    ),
    FarmTemplateId.BROWSER_TOAST_LEVEL_TOO_LOW: TemplateSpec(
        "browser_toast_level_too_low.png", threshold=0.78, region="toast"
    ),
    # Confirmation is permitted only when both the invariant beginning of the
    # target-resource expiry message and its red confirm button are visible.
    FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_TOAST: TemplateSpec(
        # Crop from the supplied 1280×720 confirmation dialog. It stops
        # before the changing countdown and amount, so only the invariant
        # prefix authorises confirmation together with its red button.
        # Current live dialog scores 0.73 because the timer and resource
        # amounts alter glyph anti-aliasing around the invariant prefix. Other
        # observed screens stay at or below 0.30, and this signal never clicks
        # alone: the red Confirm button must independently match as well.
        "browser_target_resource_expiry_toast.png", threshold=0.70,
        reference_width=1280, reference_height=720,
        scale_variants=(0.97, 1.0, 1.03), grayscale=True,
    ),
    FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_CONFIRM: TemplateSpec(
        "browser_target_resource_expiry_confirm.png", threshold=0.80,
        reference_width=1280, reference_height=720,
        scale_variants=(0.97, 1.0, 1.03),
    ),
    FarmTemplateId.BROWSER_GATHER_BUTTON_ENABLED: TemplateSpec(
        "browser_gather_button_enabled.png",
        threshold=0.80,
        region="lower",
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL: TemplateSpec(
        "browser_team_selection_panel_anchor.png",
        threshold=0.80,
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON: TemplateSpec(
        "browser_gather_button_enabled.png",
        threshold=0.80,
        region="lower",
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_TEAM_2_BADGE: TemplateSpec(
        "browser_team_2_badge.png",
        threshold=0.82,
        region="left",
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_TEAM_3_BADGE: TemplateSpec(
        "browser_team_3_badge.png",
        threshold=0.82,
        region="left",
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_TEAM_4_BADGE: TemplateSpec(
        "browser_team_4_badge.png",
        threshold=0.82,
        region="left",
        reference_width=1280,
        reference_height=720,
    ),
    FarmTemplateId.BROWSER_TEAM_SELECTED_BORDER: TemplateSpec(
        "browser_team_selected_border_anchor.png",
        threshold=0.76,
        region="left",
        reference_width=1280,
        reference_height=720,
    ),
}


class BrowserCanvasMatcher:
    """Matches the ADB template pack after scaling it to the browser canvas."""

    def __init__(self, template_root: Path | None = None) -> None:
        self.template_root = template_root or Path(__file__).with_name("assets") / "farm_templates"
        self._templates: dict[str, object] = {}

    def detect(self, screenshot_png: bytes) -> GameDetectionResult:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(screenshot_png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Canvas screenshot không phải PNG hợp lệ")
        evidence = {template_id: self._match(image, template_id) for template_id in DETECTION_TEMPLATES}
        result = BrowserGameStateDetector().detect(evidence)
        # The same compact team HUD is visible in both stable City and World
        # Map frames. Scanning only World Map left the dashboard with a stale
        # all-ready roster while the profile was visibly in City. Popup and
        # unknown screens remain excluded so unrelated text cannot leak into
        # scheduling.
        roster = (
            self._team_roster(image)
            if self._state_has_team_roster(result.state)
            else ()
        )
        return replace(
            result,
            ready_teams=tuple(row.team for row in roster if row.state == TeamRowState.READY),
            team_roster=roster,
        )

    @staticmethod
    def _state_has_team_roster(state: DetectedGameState) -> bool:
        return state in {DetectedGameState.CITY, DetectedGameState.WORLD_MAP}

    def _team_roster(self, image: object) -> tuple[TeamRosterRow, ...]:
        """Scan every unlocked roster row and classify it exactly once.

        `Sẵn sàng` is positive ready evidence. A confirmed higher row proves
        preceding rows exist (the game's unlock order); rows without that label
        are Busy. This is the same conservative inference used by the ADB
        availability service and prevents a gathering team being re-selected.
        """
        import cv2

        ready_templates = (
            self._load(_READY_TEAM_LABEL),
            self._load(_READY_TEAM_LABEL_SNOW),
        )
        busy_template = self._load(_BUSY_TEAM_LABEL)
        image_height, image_width = image.shape[:2]
        # The game reflows the roster panel between the compact grid canvas and
        # the leased 1280x720 renderer, but its status font remains essentially
        # the same device-pixel size. Scaling these 48x16 text templates with
        # the whole canvas produced ~73x27 templates at 1280x720; only one of
        # four identical "Sẵn sàng" rows then happened to match. Keep status
        # glyphs at their captured HUD size and scale only the row positions.
        status_ready = ready_templates
        status_busy = busy_template
        # Restrict every status match to the small left-side roster panel.
        # This intentionally excludes all gameplay, city HUD and popup text.
        roster_top = round(image_height * _ROSTER_TOP_RATIO)
        roster_bottom = min(image_height, round(image_height * _ROSTER_BOTTOM_RATIO))
        row_height = max(1, round(image_height * _ROSTER_ROW_HEIGHT_RATIO))
        # Never infer a row from the count/order of all template matches. A
        # match in the city HUD or another row caused exactly that bug. Probe
        # each complete row visible inside the roster panel independently;
        # portrait evidence determines the actual team count in this frame.
        label_left = round(image_width * _ROSTER_LABEL_LEFT_RATIO)
        label_right = min(round(image_width * _ROSTER_LABEL_RIGHT_RATIO), image_width)
        portrait_left = round(image_width * _ROSTER_PORTRAIT_LEFT_RATIO)
        portrait_right = min(round(image_width * _ROSTER_PORTRAIT_RIGHT_RATIO), image_width)
        ready_grays = tuple(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) for template in status_ready)
        ready_edges = tuple(cv2.Canny(template, 50, 120) for template in ready_grays)
        # Keep only the static icon/prefix of the busy label; the trailing
        # ``mm:ss`` text is intentionally excluded because it changes on each
        # capture.  This is especially important for rows 1/2 in the live HUD.
        busy_indicator = status_busy[:, : min(_BUSY_TEAM_INDICATOR_WIDTH, status_busy.shape[1])]
        busy_gray = cv2.cvtColor(busy_indicator, cv2.COLOR_BGR2GRAY)
        states: dict[int, TeamRowState] = {}
        visible_teams: list[int] = []
        visible_row_count = max(0, (roster_bottom - roster_top) // row_height)
        for team in range(1, visible_row_count + 1):
            row_top = roster_top + (team - 1) * row_height
            row_bottom = min(roster_bottom, row_top + row_height)
            # A status-like patch alone cannot create a team row. On a
            # three-team account the collapse arrow/background directly below
            # row 3 scores highly against the small grayscale Ready template.
            # Portraits have a dense, stable set of edges across skins, while
            # that empty footer does not. Require the portrait slot first.
            portrait = image[row_top:row_bottom, portrait_left:portrait_right]
            if portrait.size == 0:
                continue
            portrait_gray = cv2.cvtColor(portrait, cv2.COLOR_BGR2GRAY)
            portrait_edges = cv2.Canny(portrait_gray, 60, 140)
            portrait_edge_density = cv2.countNonZero(portrait_edges) / portrait_gray.size
            if portrait_edge_density < _ROSTER_PORTRAIT_MIN_EDGE_DENSITY:
                continue
            visible_teams.append(team)
            search = image[row_top:row_bottom, label_left:label_right]
            if any(template.shape[1] > search.shape[1] or template.shape[0] > search.shape[0] for template in status_ready):
                continue
            search_gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
            ready_score = max(
                cv2.minMaxLoc(cv2.matchTemplate(search_gray, template, cv2.TM_CCOEFF_NORMED))[1]
                for template in ready_grays
            )
            search_edges = cv2.Canny(search_gray, 50, 120)
            ready_edge_score = max(
                cv2.minMaxLoc(cv2.matchTemplate(search_edges, template, cv2.TM_CCOEFF_NORMED))[1]
                for template in ready_edges
            )
            busy_score = -1.0
            # The icon starts at the fixed left edge of the label and the HUD
            # baseline is stable within each row. Restrict its search band to
            # that position: allowing it to slide across the whole label made
            # plain Ready text/background falsely correlate with Busy.
            # Labels start roughly 16px into this cropped slot; keep a narrow
            # horizontal band rather than the full text field.
            busy_band = search[12:36, :52]
            if (
                busy_indicator.shape[1] <= busy_band.shape[1]
                and busy_indicator.shape[0] <= busy_band.shape[0]
            ):
                busy_score = cv2.minMaxLoc(
                    cv2.matchTemplate(
                        cv2.cvtColor(busy_band, cv2.COLOR_BGR2GRAY),
                        busy_gray,
                        cv2.TM_CCOEFF_NORMED,
                    )
                )[1]
            # A row is Ready only with strong positive evidence and when that
            # evidence is decisively stronger than the observed busy label.
            # This excludes the sidebar's repeated background which used to
            # make every row score as `Sẵn sàng` at the old 0.66 threshold.
            if busy_score >= _BUSY_TEAM_MIN_SCORE:
                states[team] = TeamRowState.BUSY
            elif self._is_ready_team_label(ready_score, ready_edge_score, busy_score):
                states[team] = TeamRowState.READY
            elif busy_score >= 0.55 and busy_score >= ready_score + 0.05:
                states[team] = TeamRowState.BUSY
        if not visible_teams:
            return ()
        # Team slots unlock in order. Use the final portrait actually visible
        # in this screenshot as the roster length. A visible row whose status
        # text is mid-animation remains conservatively Busy rather than being
        # dropped and changing the apparent number of teams.
        highest_visible = max(visible_teams)
        return tuple(
            TeamRosterRow(
                team=team,
                state=states.get(team, TeamRowState.BUSY),
                evidence=(
                    "ReadyLabel"
                    if states.get(team) is TeamRowState.READY
                    else "BusyLabel"
                    if states.get(team) is TeamRowState.BUSY
                    else "InferredPrecedingRow"
                ),
            )
            for team in range(1, highest_visible + 1)
        )

    @staticmethod
    def _is_ready_team_label(
        grayscale_score: float,
        edge_score: float,
        busy_score: float,
    ) -> bool:
        """Require both the tone and glyph shape of the Vietnamese label."""
        return (
            busy_score < _BUSY_TEAM_MIN_SCORE
            and grayscale_score >= _READY_TEAM_MIN_GRAYSCALE_SCORE
            and edge_score >= _READY_TEAM_MIN_EDGE_SCORE
            and grayscale_score >= busy_score + 0.06
        )

    @staticmethod
    def _team_for_ready_label(label_top: int, roster_top: int, row_height: int) -> int | None:
        if row_height <= 0:
            return None
        # A label is vertically centered within its row.  Offset half a row,
        # then use integer division so the four visual rows stay 1→2→3→4.
        team = ((label_top - roster_top) + row_height // 2) // row_height + 1
        return team if team >= 1 else None

    def _match(self, image: object, template_id: FarmTemplateId) -> TemplateEvidence:
        spec = SPECS.get(template_id)
        if spec is None:
            return TemplateEvidence(template_id, False)
        candidates = tuple(
            self._match_template(image, template_id, spec, filename)
            for filename in (spec.filename, *spec.alternatives)
        )
        return max(candidates, key=lambda evidence: evidence.confidence)

    def _match_template(
        self,
        image: object,
        template_id: FarmTemplateId,
        spec: TemplateSpec,
        filename: str,
    ) -> TemplateEvidence:
        import cv2

        template = self._load(filename)
        image_height, image_width = image.shape[:2]
        scale_x = image_width / spec.reference_width
        scale_y = scale_x if spec.uniform_width_scale else image_height / spec.reference_height
        x, y, width, height = self._region(spec.region, image_width, image_height)
        search = image[y : y + height, x : x + width]
        best_confidence = -1.0
        best_location: tuple[int, int] | None = None
        best_size: tuple[int, int] | None = None
        for variant in spec.scale_variants:
            scaled_width = max(1, round(template.shape[1] * scale_x * variant))
            scaled_height = max(1, round(template.shape[0] * scale_y * variant))
            if scaled_width > search.shape[1] or scaled_height > search.shape[0]:
                continue
            scaled = cv2.resize(template, (scaled_width, scaled_height), interpolation=cv2.INTER_AREA)
            if spec.edge:
                match_search = cv2.Canny(cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), 60, 150)
                match_template = cv2.Canny(cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY), 60, 150)
            elif spec.grayscale:
                match_search = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
                match_template = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
            else:
                match_search = search
                match_template = scaled
            result = cv2.matchTemplate(match_search, match_template, cv2.TM_CCOEFF_NORMED)
            _, confidence, _, location = cv2.minMaxLoc(result)
            if confidence > best_confidence:
                best_confidence = float(confidence)
                best_location = (int(location[0]), int(location[1]))
                best_size = (scaled_width, scaled_height)
        if best_confidence < spec.threshold or best_location is None or best_size is None:
            return TemplateEvidence(template_id, False, max(0.0, best_confidence))
        return TemplateEvidence(
            template_id,
            True,
            best_confidence,
            (x + best_location[0], y + best_location[1], best_size[0], best_size[1]),
        )

    def _load(self, filename: str) -> object:
        import cv2

        if filename not in self._templates:
            path = self.template_root / filename
            template = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if template is None:
                raise FileNotFoundError(f"Thiếu farm template: {path}")
            self._templates[filename] = template
        return self._templates[filename]

    @staticmethod
    def _region(name: str, width: int, height: int) -> tuple[int, int, int, int]:
        if name == "left": return 0, 0, width // 4, height
        # The City control is anchored in this compact corner slot. Keeping
        # its search area separate prevents similar castle/terrain artwork in
        # the rest of the game canvas from becoming City evidence.
        if name == "city_corner": return 0, height * 3 // 4, width // 6, height - (height * 3 // 4)
        if name == "map_corner": return 0, height * 3 // 4, width * 3 // 25, height - (height * 3 // 4)
        if name == "lower_left": return 0, height // 2, width // 2, height - height // 2
        if name == "lower": return 0, height // 2, width, height - height // 2
        if name == "lower_right": return width // 2, height // 2, width - width // 2, height - height // 2
        # The resource-search checkbox occupies a fixed, compact slot around
        # (941, 512) on the leased 1280x720 renderer.  A generic lower-right
        # search also matched circular HUD art near (1125, 393), causing a
        # click far away from the option. Keep both unchecked and checked
        # evidence inside the actual checkbox/label row.
        if name == "search_checkbox":
            left = width * 7 // 10
            top = height * 2 // 3
            return left, top, width // 10, height // 9
        # The search level readout (``Cấp: n / n``) is centered above the
        # slider in the resource panel. Restricting the two digit templates
        # to this slot prevents similar HUD numbers elsewhere from becoming
        # level evidence.
        if name == "resource_level":
            left = width * 3 // 10
            top = height * 4 // 5
            return left, top, width // 4, height - top
        if name == "top_left": return 0, 0, width // 4, height // 5
        if name == "toast": return width // 6, height // 15, width * 2 // 3, height // 3
        if name == "right": return width * 3 // 4, 0, width - (width * 3 // 4), height // 2
        if name == "center": return width // 4, height // 5, width // 2, height - (height * 2 // 5)
        return 0, 0, width, height
