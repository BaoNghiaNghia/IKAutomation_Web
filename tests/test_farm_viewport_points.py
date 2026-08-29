from ik_chrome_auto.runner import (
    AUTOMATION_RENDERER_SIZE,
    FARM_MINIMUM_CANVAS_SIZE,
    FARM_REFERENCE_ASPECT_RATIO,
    ProfileWorker,
)


def test_farm_layout_fallbacks_use_relative_canvas_positions_at_16_by_9() -> None:
    """Fallback controls retain their 16:9 locations without desktop pixels."""
    assert FARM_REFERENCE_ASPECT_RATIO == 16 / 9
    assert ProfileWorker._resource_button_layout_bounds("wood", (1280, 720)) == (459, 464, 122, 130)
    assert ProfileWorker._world_map_search_layout_bounds((1280, 720)) == (379, 544, 74, 75)
    assert ProfileWorker._search_target_checkbox_layout_bounds((1280, 720)) == (892, 493, 51, 47)


def test_farm_layout_fallbacks_scale_for_a_compact_five_profile_viewport() -> None:
    """Each profile uses its own captured canvas rather than screen geometry."""
    assert ProfileWorker._resource_button_layout_bounds("wood", (384, 216)) == (138, 139, 36, 39)
    assert ProfileWorker._world_map_search_layout_bounds((384, 216)) == (106, 156, 38, 36)
    assert ProfileWorker._search_target_checkbox_layout_bounds((384, 216)) == (265, 145, 20, 20)


def test_farm_rejects_tiny_renderer_captures_before_team_or_resource_input() -> None:
    """A tiny canvas cannot safely distinguish Ready from Busy labels."""
    assert AUTOMATION_RENDERER_SIZE == (1280, 720)
    assert FARM_MINIMUM_CANVAS_SIZE == (1280, 720)
    assert ProfileWorker._farm_canvas_is_usable((1280, 720)) is True
    assert ProfileWorker._farm_canvas_is_usable((640, 360)) is False
    assert ProfileWorker._farm_canvas_is_usable((366, 168)) is False
    assert ProfileWorker._farm_canvas_is_usable((186, 66)) is False


def test_continent_coordinate_fields_use_canvas_ratio_offsets() -> None:
    # The pin itself is matched live. The two input fields are offset from it
    # by normalized canvas distances, which gives the same target for any
    # profile viewport.
    assert ProfileWorker._coordinate_fields_from_pin((400, 100, 40, 40), (1280, 720)) == (
        (286, 120, 2, 2),
        (420, 66, 2, 2),
    )
    assert ProfileWorker._coordinate_fields_from_pin((120, 50, 20, 20), (384, 216)) == (
        (90, 60, 2, 2),
        (130, 44, 2, 2),
    )
