from __future__ import annotations

import sys

import pytest

from ik_chrome_auto.game2048 import decode_png
from ik_chrome_auto.windows import (
    WindowRect,
    calculate_tiled_positions,
    capture_screen_region_png,
    descendant_process_ids,
    encode_rgb_png,
)


def test_descendant_process_ids_collects_full_tree_without_cycles() -> None:
    parents = {11: 10, 12: 10, 13: 11, 14: 13, 10: 14, 99: 50}

    assert descendant_process_ids(10, parents) == (10, 11, 12, 13, 14)


def test_calculate_tiled_positions_orders_left_to_right_then_down() -> None:
    work_area = WindowRect(0, 0, 1200, 800)

    positions = calculate_tiled_positions(work_area, 500, 300, 5, gap=10)

    assert positions == [
        (0, 0),
        (510, 0),
        (0, 310),
        (510, 310),
        (28, 28),
    ]


def test_calculate_tiled_positions_honors_offset_work_area() -> None:
    work_area = WindowRect(100, 50, 900, 650)

    positions = calculate_tiled_positions(work_area, 400, 300, 2, gap=0)

    assert positions == [(100, 50), (500, 50)]


def test_rgb_png_encoder_round_trips_exact_pixels() -> None:
    rgb = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 250, 240, 230))
    image = decode_png(encode_rgb_png(2, 2, rgb))

    assert (image.width, image.height) == (2, 2)
    assert image.pixels == rgb


@pytest.mark.skipif(sys.platform != "win32", reason="Windows GDI integration")
def test_gdi_screen_capture_returns_requested_size() -> None:
    try:
        png = capture_screen_region_png(WindowRect(0, 0, 8, 8))
    except PermissionError:
        pytest.skip("desktop capture is blocked by the test sandbox")
    image = decode_png(png)

    assert (image.width, image.height) == (8, 8)
