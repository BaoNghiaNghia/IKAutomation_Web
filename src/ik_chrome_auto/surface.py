"""Shared game-surface geometry for the 1280x720 automation renderer.

Split out of ``browser.py`` so Farm, Monitor and input share one origin
without duplicating box math across god-files. Coordinates are always
canvas-relative ratios; desktop/iframe offsets are deliberately discarded.
"""
from __future__ import annotations

GAME_SURFACE_WIDTH = 1280.0
GAME_SURFACE_HEIGHT = 720.0


def fixed_game_surface_box() -> dict[str, float]:
    """Coordinate system used while automation rerenders at 720p."""
    return {"x": 0.0, "y": 0.0, "width": GAME_SURFACE_WIDTH, "height": GAME_SURFACE_HEIGHT}


def origin_surface_box(box: dict[str, float]) -> dict[str, float]:
    """Keep live surface dimensions while discarding iframe offsets."""
    return {
        "x": 0.0,
        "y": 0.0,
        "width": max(1.0, float(box["width"])),
        "height": max(1.0, float(box["height"])),
    }
