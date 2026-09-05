from __future__ import annotations

import pytest

from ik_chrome_auto.interaction import (
    calculate_target_point,
    format_coordinate,
    validate_viewport,
)


def test_validate_viewport() -> None:
    assert validate_viewport(1280, 720) == (1280, 720)
    with pytest.raises(ValueError, match="Chiều rộng"):
        validate_viewport(100, 720)


def test_calculate_canvas_target_point() -> None:
    event = {"canvas": {"ratio_x": 0.25, "ratio_y": 0.75}}
    box = {"x": 100.0, "y": 50.0, "width": 800.0, "height": 400.0}

    assert calculate_target_point(event, box) == (300.0, 350.0)


def test_maximized_master_maps_to_same_logical_point_on_compact_follower() -> None:
    # The source click was captured at (1440, 540) on a 1920x1080 master.
    # Its raw master pixels must never leak into the follower transform.
    event = {
        "canvas": {
            "ratio_x": 0.75,
            "ratio_y": 0.5,
            "css_x": 1440.0,
            "css_y": 540.0,
            "css_width": 1920.0,
            "css_height": 1080.0,
            "backing_width": 1280,
            "backing_height": 720,
        }
    }
    compact_follower = {
        "x": 327.0,
        "y": 211.0,
        "width": 500.0,
        "height": 281.0,
    }

    assert calculate_target_point(event, compact_follower) == (702.0, 351.5)


def test_calculate_viewport_target_clamps_ratio() -> None:
    event = {"viewport": {"ratio_x": 2.0, "ratio_y": -1.0}}
    box = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 50.0}

    assert calculate_target_point(event, box) == (110.0, 20.0)


def test_format_canvas_coordinate() -> None:
    event = {
        "canvas": {
            "pixel_x_rounded": 640,
            "pixel_y_rounded": 360,
            "css_x": 640,
            "css_y": 360,
            "backing_width": 1280,
            "backing_height": 720,
        }
    }

    result = format_coordinate("farm-1", event)

    assert "canvas px=(640, 360)" in result
    assert "1280x720" in result
