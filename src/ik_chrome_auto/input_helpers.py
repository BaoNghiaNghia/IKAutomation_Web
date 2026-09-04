"""Shared geometry helpers for renderer-local input."""
from __future__ import annotations

from dataclasses import dataclass


GAME_REFERENCE_WIDTH = 1280.0
GAME_REFERENCE_HEIGHT = 720.0


@dataclass(frozen=True)
class CanvasReferencePoint:
    """One logical game point whose origin is the 1280x720 canvas top-left."""

    x: float
    y: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.x <= GAME_REFERENCE_WIDTH:
            raise ValueError("Game reference X nằm ngoài canvas 1280")
        if not 0.0 <= self.y <= GAME_REFERENCE_HEIGHT:
            raise ValueError("Game reference Y nằm ngoài canvas 720")

    @property
    def ratio(self) -> tuple[float, float]:
        return self.x / GAME_REFERENCE_WIDTH, self.y / GAME_REFERENCE_HEIGHT


@dataclass(frozen=True)
class CanvasTransformSnapshot:
    """Fresh mapping from the canonical game canvas to live CSS coordinates."""

    viewport_left: float
    viewport_top: float
    css_width: float
    css_height: float

    def __post_init__(self) -> None:
        if self.css_width <= 0 or self.css_height <= 0:
            raise ValueError("Kích thước canvas thực tế không hợp lệ")

    @classmethod
    def from_box(cls, box: dict[str, float]) -> "CanvasTransformSnapshot":
        return cls(
            viewport_left=float(box.get("x", 0.0)),
            viewport_top=float(box.get("y", 0.0)),
            css_width=float(box["width"]),
            css_height=float(box["height"]),
        )

    def to_local(self, point: CanvasReferencePoint) -> tuple[float, float]:
        x_ratio, y_ratio = point.ratio
        return self.css_width * x_ratio, self.css_height * y_ratio

    def to_viewport(self, point: CanvasReferencePoint) -> tuple[float, float]:
        local_x, local_y = self.to_local(point)
        return self.viewport_left + local_x, self.viewport_top + local_y


def control_center_reference_point(
    bounds: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> CanvasReferencePoint:
    """Map fresh capture bounds into the one canonical 1280x720 origin."""
    left, top, width, height = bounds
    image_width, image_height = image_size
    if width <= 0 or height <= 0 or image_width <= 0 or image_height <= 0:
        raise ValueError("Game control bounds không hợp lệ")
    if left < 0 or top < 0 or left + width > image_width or top + height > image_height:
        raise ValueError("Game control bounds nằm ngoài ảnh game vừa chụp")
    return CanvasReferencePoint(
        (left + width / 2) * GAME_REFERENCE_WIDTH / image_width,
        (top + height / 2) * GAME_REFERENCE_HEIGHT / image_height,
    )


def control_center_ratio(
    bounds: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> tuple[float, float]:
    """Return the normalized centre of verified bounds in a fresh capture.

    Input coordinates in this project are always relative to the captured
    game surface. Desktop and iframe offsets must never be added here.
    """
    return control_center_reference_point(bounds, image_size).ratio
