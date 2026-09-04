from __future__ import annotations

import cv2
import numpy as np
import pytest

from ik_chrome_auto.farm_matcher import BrowserCanvasMatcher
from ik_chrome_auto.farm_vision import DetectedGameState, TeamRowState, TemplateEvidence


def test_city_template_search_is_limited_to_bottom_left_corner() -> None:
    assert BrowserCanvasMatcher._region("city_corner", 835, 432) == (0, 324, 139, 108)
    assert BrowserCanvasMatcher._region("map_corner", 1280, 720) == (0, 540, 153, 180)
from ik_chrome_auto.farm_vision import FarmTemplateId


def test_selective_detection_matches_only_requested_templates_and_skips_roster(monkeypatch) -> None:
    matcher = BrowserCanvasMatcher()
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    calls: list[FarmTemplateId] = []

    def match(_image, template_id: FarmTemplateId) -> TemplateEvidence:
        calls.append(template_id)
        return TemplateEvidence(
            template_id,
            template_id == FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
            bounds=(10, 10, 20, 20),
        )

    monkeypatch.setattr(matcher, "_match", match)
    monkeypatch.setattr(
        matcher,
        "_team_roster",
        lambda _image: pytest.fail("roster scan must be skipped"),
    )

    result = matcher.detect(
        encoded.tobytes(),
        template_ids=(FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,),
        include_roster=False,
    )

    assert result.state == DetectedGameState.CITY
    assert calls == [FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON]
    assert not result.ready_teams


def test_browser_map_toggle_templates_match_their_actual_direction() -> None:
    from ik_chrome_auto.farm_matcher import SPECS

    assert SPECS[FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON].filename == "browser_open_world_map_1280.png"
    assert SPECS[FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON].filename == "browser_city_return_green_1280.png"


@pytest.mark.parametrize(
    ("filename", "left", "top"),
    (
        ("browser_city_return_green_1280.png", 28, 595),
        ("browser_city_return_red_1280.png", 34, 587),
    ),
)
def test_full_resolution_city_return_button_matches_green_and_red_skins(
    filename: str,
    left: int,
    top: int,
) -> None:
    matcher = BrowserCanvasMatcher()
    template = matcher._load(filename)
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    canvas[top : top + height, left : left + width] = template

    evidence = matcher._match(canvas, FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON)

    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (left, top)


def test_full_resolution_world_map_button_matches_city_canvas() -> None:
    matcher = BrowserCanvasMatcher()
    template = matcher._load("browser_open_world_map_1280.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
    top, left = 592, 20
    height, width = template.shape[:2]
    canvas[top : top + height, left : left + width] = template

    evidence = matcher._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)

    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (left, top)


def test_matcher_scales_template_and_returns_canvas_relative_bounds(tmp_path) -> None:
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (27, 17), (255, 255, 255), 2)
    cv2.line(template, (0, 19), (29, 0), (80, 160, 220), 2)
    cv2.imwrite(str(tmp_path / "world_map_anchor.png"), template)
    canvas = np.zeros((360, 640, 3), dtype=np.uint8)
    scaled = cv2.resize(template, (15, 10), interpolation=cv2.INTER_AREA)
    canvas[240:250, 100:115] = scaled
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    matcher = BrowserCanvasMatcher(tmp_path)
    evidence = matcher._match(cv2.imdecode(encoded, cv2.IMREAD_COLOR), FarmTemplateId.WORLD_MAP_ANCHOR)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (100, 240)


def test_matcher_scales_height_independently_for_stretched_browser_canvas(tmp_path) -> None:
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (27, 17), (255, 255, 255), 2)
    cv2.line(template, (0, 19), (29, 0), (80, 160, 220), 2)
    cv2.imwrite(str(tmp_path / "world_map_anchor.png"), template)
    # 640x300 has different horizontal and vertical scale factors from the
    # 1280x720 template source, as happens with a browser-hosted canvas.
    canvas = np.zeros((300, 640, 3), dtype=np.uint8)
    scaled = cv2.resize(template, (15, 8), interpolation=cv2.INTER_AREA)
    canvas[220:228, 100:115] = scaled
    matcher = BrowserCanvasMatcher(tmp_path)
    evidence = matcher._match(canvas, FarmTemplateId.WORLD_MAP_ANCHOR)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (100, 220)


def test_full_renderer_roster_keeps_status_text_at_hud_pixel_size() -> None:
    matcher = BrowserCanvasMatcher()
    ready = matcher._load("browser_ready_team_label.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)

    # The roster moves with the 1280x720 layout, while the game keeps this
    # small status font at its native HUD pixel size.
    for team in range(1, 5):
        top = round(720 * 0.425) + (team - 1) * round(720 * 0.071) + 16
        left = 78
        height, width = ready.shape[:2]
        canvas[top : top + height, left : left + width] = ready
        portrait_top = round(720 * 0.425) + (team - 1) * round(720 * 0.071)
        # Deterministic textured pixels model the dense edges in a real hero
        # portrait (a one-pixel checkerboard is suppressed by Canny).
        portrait = np.random.default_rng(team).integers(0, 256, (51, 52), dtype=np.uint8)
        canvas[portrait_top : portrait_top + 51, 18:70] = portrait[..., None]

    roster = matcher._team_roster(canvas)

    assert tuple((row.team, row.state) for row in roster) == (
        (1, TeamRowState.READY),
        (2, TeamRowState.READY),
        (3, TeamRowState.READY),
        (4, TeamRowState.READY),
    )


def test_full_renderer_roster_uses_direct_busy_evidence_per_row() -> None:
    matcher = BrowserCanvasMatcher()
    ready = matcher._load("browser_ready_team_label.png")
    busy = matcher._load("browser_busy_team_label.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)

    for team, template in ((1, ready), (2, busy), (3, ready), (4, ready)):
        top = round(720 * 0.425) + (team - 1) * round(720 * 0.071) + 16
        left = 78
        height, width = template.shape[:2]
        canvas[top : top + height, left : left + width] = template
        portrait_top = round(720 * 0.425) + (team - 1) * round(720 * 0.071)
        portrait = np.random.default_rng(team).integers(0, 256, (51, 52), dtype=np.uint8)
        canvas[portrait_top : portrait_top + 51, 18:70] = portrait[..., None]

    roster = matcher._team_roster(canvas)

    assert tuple((row.team, row.state) for row in roster) == (
        (1, TeamRowState.READY),
        (2, TeamRowState.BUSY),
        (3, TeamRowState.READY),
        (4, TeamRowState.READY),
    )


def test_busy_resource_text_cannot_become_ready_from_grayscale_correlation_alone() -> None:
    # Scores measured from the reported 1280x720 account-2 capture. The busy
    # first row resembles the tiny Ready template in grayscale, but its glyph
    # edges do not. Rows 2 and 3 contain the real `Sẵn sàng` label.
    classify = BrowserCanvasMatcher._is_ready_team_label

    assert classify(0.7738, 0.2205, 0.5448) is False
    # Live account-2 row 1 (busy) previously slipped through by only 0.002 on
    # the edge threshold and made all three dashboard dots green.
    assert classify(0.7785, 0.2424, 0.5451) is False
    assert classify(0.8186, 0.2610, 0.0660) is True
    assert classify(0.8563, 0.2584, 0.1045) is True
    # Post-dispatch account-2 capture: the antialiased World Map label has
    # weaker edges than City, but its tone is strong and no busy prefix exists.
    assert classify(0.8200, 0.2167, 0.0735) is True
    assert classify(0.7556, 0.2512, 0.6734) is False
    # Account-1 snowy World Map row 4 from the reported debug frame. The
    # genuine Ready label is softer, while its Busy-prefix score is absent.
    assert classify(0.7726, 0.1915, 0.1892) is True


def test_roster_is_refreshed_on_stable_city_and_world_map_only() -> None:
    supports = BrowserCanvasMatcher._state_has_team_roster

    assert supports(DetectedGameState.CITY) is True
    assert supports(DetectedGameState.WORLD_MAP) is True
    assert supports(DetectedGameState.RESOURCE_SEARCH_PANEL) is False
    assert supports(DetectedGameState.UNKNOWN) is False


def test_busy_row_uses_static_indicator_when_countdown_changes() -> None:
    matcher = BrowserCanvasMatcher()
    ready = matcher._load("browser_ready_team_label.png")
    busy = matcher._load("browser_busy_team_label.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)

    for team in range(1, 4):
        row_top = round(720 * 0.425) + (team - 1) * round(720 * 0.071)
        portrait = np.random.default_rng(team + 10).integers(0, 256, (51, 52), dtype=np.uint8)
        canvas[row_top : row_top + 51, 18:70] = portrait[..., None]
        if team == 2:
            # Retain the icon/static prefix but replace the live countdown.
            canvas[row_top + 16 : row_top + 32, 78:104] = busy[:, :26]
            canvas[row_top + 16 : row_top + 32, 104:128] = np.random.default_rng(99).integers(
                0, 256, (16, 24, 3), dtype=np.uint8
            )
        else:
            canvas[row_top + 16 : row_top + 32, 78:126] = ready

    roster = matcher._team_roster(canvas)

    assert tuple((row.team, row.state) for row in roster) == (
        (1, TeamRowState.READY),
        (2, TeamRowState.BUSY),
        (3, TeamRowState.READY),
    )


def test_active_resource_templates_are_locked_to_the_1280_renderer() -> None:
    """Selected-resource templates include each supplied gold-ring state."""
    import ik_chrome_auto.farm_matcher as matcher_module

    expected = {
        FarmTemplateId.BROWSER_FOOD_RESOURCE_ACTIVE: "browser_resource_food_active_1280.png",
        FarmTemplateId.BROWSER_WOOD_RESOURCE_ACTIVE: "browser_resource_wood_active_1280.png",
        FarmTemplateId.BROWSER_STONE_RESOURCE_ACTIVE: "browser_resource_stone_active_1280.png",
        FarmTemplateId.BROWSER_IRON_RESOURCE_ACTIVE: "browser_resource_iron_active_1280.png",
    }

    for template_id, filename in expected.items():
        spec = matcher_module.SPECS[template_id]
        assert spec.filename == filename
        assert (spec.reference_width, spec.reference_height) == (1280, 720)
        assert spec.scale_variants == (1.0,)


def test_search_button_uses_the_latest_supplied_live_capture() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    spec = matcher_module.SPECS[FarmTemplateId.BROWSER_SEARCH_BUTTON_ENABLED]

    assert spec.filename == "browser_search_button_enabled.png"
    assert (spec.reference_width, spec.reference_height) == (1280, 720)
    assert spec.uniform_width_scale is False


def test_world_map_magnifier_uses_the_latest_supplied_crop_at_720p() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    spec = matcher_module.SPECS[FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON]

    assert spec.filename == "browser_resource_search_button.png"
    assert (spec.reference_width, spec.reference_height) == (1280, 720)
    assert spec.threshold == 0.76


def test_checked_search_target_template_requires_positive_tick_evidence() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    spec = matcher_module.SPECS[FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_CHECKED]

    assert spec.filename == "browser_search_target_checkbox_checked.png"
    assert (spec.reference_width, spec.reference_height) == (1014, 275)
    assert spec.uniform_width_scale is True
    assert spec.threshold == 0.70
    assert spec.region == "search_checkbox"


def test_unchecked_search_target_template_uses_the_same_tight_checkbox_region() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    spec = matcher_module.SPECS[FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_UNCHECKED]

    assert spec.region == "search_checkbox"
    assert matcher_module.BrowserCanvasMatcher._region("search_checkbox", 1280, 720) == (
        896,
        480,
        128,
        80,
    )


def test_resource_level_templates_use_the_renderer_and_exact_panel_slot() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    for template_id in (
        FarmTemplateId.BROWSER_RESOURCE_LEVEL_6,
        FarmTemplateId.BROWSER_RESOURCE_LEVEL_7,
    ):
        spec = matcher_module.SPECS[template_id]
        assert (spec.reference_width, spec.reference_height) == (1280, 720)
        assert spec.region == "resource_level"
        assert spec.scale_variants == (0.97, 1.0, 1.03)


@pytest.mark.parametrize(
    ("level", "template_id"),
    (
        (6, FarmTemplateId.BROWSER_RESOURCE_LEVEL_6),
        (7, FarmTemplateId.BROWSER_RESOURCE_LEVEL_7),
    ),
)
def test_resource_level_is_matched_at_its_1280_panel_position(
    level: int,
    template_id: FarmTemplateId,
) -> None:
    matcher = BrowserCanvasMatcher()
    template = matcher._load(f"browser_resource_level_{level}.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
    height, width = template.shape[:2]
    canvas[630 : 630 + height, 540 : 540 + width] = template

    evidence = matcher._match(canvas, template_id)

    assert evidence.found
    assert evidence.bounds == (540, 630, width, height)


def test_team_selection_templates_use_the_supplied_1280_renderer_capture() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    for template_id in (
        FarmTemplateId.BROWSER_GATHER_BUTTON_ENABLED,
        FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL,
        FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON,
        FarmTemplateId.BROWSER_TEAM_2_BADGE,
        FarmTemplateId.BROWSER_TEAM_3_BADGE,
        FarmTemplateId.BROWSER_TEAM_4_BADGE,
        FarmTemplateId.BROWSER_TEAM_SELECTED_BORDER,
    ):
        spec = matcher_module.SPECS[template_id]
        assert (spec.reference_width, spec.reference_height) == (1280, 720)


def test_expiring_resource_confirmation_uses_the_supplied_1280_dialog() -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    for template_id in (
        FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_TOAST,
        FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_CONFIRM,
    ):
        spec = matcher_module.SPECS[template_id]
        assert (spec.reference_width, spec.reference_height) == (1280, 720)

    # The current live prefix scores about 0.73; confirmation remains guarded
    # independently by the exact red button template.
    assert (
        matcher_module.SPECS[
            FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_TOAST
        ].threshold
        == 0.70
    )


def test_status_like_footer_without_portrait_does_not_create_fourth_team() -> None:
    matcher = BrowserCanvasMatcher()
    ready = matcher._load("browser_ready_team_label.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
    portrait = np.random.default_rng(7).integers(0, 256, (51, 52), dtype=np.uint8)

    for team in range(1, 4):
        row_top = round(720 * 0.425) + (team - 1) * round(720 * 0.071)
        height, width = ready.shape[:2]
        canvas[row_top + 16 : row_top + 16 + height, 78 : 78 + width] = ready
        canvas[row_top : row_top + 51, 18:70] = portrait[..., None]

    # Reproduce a false Ready-like patch below the final portrait. The row is
    # still absent and must not appear in the scheduler or dashboard dots.
    fourth_top = round(720 * 0.425) + 3 * round(720 * 0.071)
    height, width = ready.shape[:2]
    canvas[fourth_top + 4 : fourth_top + 4 + height, 100 : 100 + width] = ready

    roster = matcher._team_roster(canvas)

    assert tuple((row.team, row.state) for row in roster) == (
        (1, TeamRowState.READY),
        (2, TeamRowState.READY),
        (3, TeamRowState.READY),
    )


def test_roster_length_comes_from_visible_portraits_not_status_match_count() -> None:
    matcher = BrowserCanvasMatcher()
    ready = matcher._load("browser_ready_team_label.png")
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)

    # The screenshot contains three unlocked team portraits, but only the
    # first status label is stable in this frame. Rows 2/3 must remain in the
    # roster as conservatively Busy instead of collapsing its length to one.
    for team in range(1, 4):
        row_top = round(720 * 0.425) + (team - 1) * round(720 * 0.071)
        portrait = np.random.default_rng(team + 30).integers(0, 256, (51, 52), dtype=np.uint8)
        canvas[row_top : row_top + 51, 18:70] = portrait[..., None]
    height, width = ready.shape[:2]
    first_top = round(720 * 0.425)
    canvas[first_top + 16 : first_top + 16 + height, 78 : 78 + width] = ready

    roster = matcher._team_roster(canvas)

    assert tuple((row.team, row.state) for row in roster) == (
        (1, TeamRowState.READY),
        (2, TeamRowState.BUSY),
        (3, TeamRowState.BUSY),
    )


def test_matcher_honours_browser_template_reference_dimensions(tmp_path, monkeypatch) -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (27, 17), (255, 255, 255), 2)
    cv2.imwrite(str(tmp_path / "browser-city.png"), template)
    monkeypatch.setitem(
        matcher_module.SPECS,
        FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
        matcher_module.TemplateSpec("browser-city.png", threshold=0.78, region="lower_left", reference_width=800, reference_height=400),
    )
    canvas = np.zeros((200, 400, 3), dtype=np.uint8)
    scaled = cv2.resize(template, (15, 10), interpolation=cv2.INTER_AREA)
    canvas[150:160, 90:105] = scaled
    evidence = BrowserCanvasMatcher(tmp_path)._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (90, 150)


def test_matcher_uses_the_best_city_skin_variant(tmp_path, monkeypatch) -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    green = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(green, (2, 2), (27, 17), (0, 220, 0), 2)
    cv2.line(green, (0, 19), (29, 0), (0, 120, 0), 2)
    snow = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(snow, (2, 2), (27, 17), (220, 180, 120), 2)
    cv2.line(snow, (0, 0), (29, 19), (255, 255, 255), 2)
    cv2.imwrite(str(tmp_path / "green.png"), green)
    cv2.imwrite(str(tmp_path / "snow.png"), snow)
    monkeypatch.setitem(
        matcher_module.SPECS,
        FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
        matcher_module.TemplateSpec("green.png", threshold=0.78, region="lower_left", reference_width=400, reference_height=200, alternatives=("snow.png",)),
    )
    canvas = np.zeros((200, 400, 3), dtype=np.uint8)
    canvas[150:170, 90:120] = snow
    evidence = BrowserCanvasMatcher(tmp_path)._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (90, 150)


def test_city_edge_match_is_not_dependent_on_environment_colour(tmp_path, monkeypatch) -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    city = np.zeros((24, 28, 3), dtype=np.uint8)
    cv2.circle(city, (14, 13), 10, (40, 180, 40), 2)
    cv2.rectangle(city, (9, 6), (19, 18), (50, 210, 70), 2)
    # Same shape, intentionally different night/snow palette.
    night_city = np.zeros((24, 28, 3), dtype=np.uint8)
    cv2.circle(night_city, (14, 13), 10, (190, 80, 210), 2)
    cv2.rectangle(night_city, (9, 6), (19, 18), (225, 95, 230), 2)
    cv2.imwrite(str(tmp_path / "city.png"), city)
    monkeypatch.setitem(
        matcher_module.SPECS,
        FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
        matcher_module.TemplateSpec(
            "city.png", threshold=0.55, region="lower_left",
            reference_width=400, reference_height=200, edge=True,
        ),
    )
    canvas = np.zeros((200, 400, 3), dtype=np.uint8)
    canvas[150:174, 90:118] = night_city
    evidence = BrowserCanvasMatcher(tmp_path)._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (90, 150)
