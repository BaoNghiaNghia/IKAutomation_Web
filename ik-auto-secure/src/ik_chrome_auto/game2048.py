from __future__ import annotations

import colorsys
import math
import struct
import time
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence


Board = tuple[tuple[int, int, int, int], ...]
DIRECTIONS = ("left", "down", "right", "up")


@dataclass(frozen=True, slots=True)
class MoveResult:
    board: Board
    changed: bool
    score: int


@dataclass(frozen=True, slots=True)
class GridDetection:
    x_lines: tuple[int, int, int, int, int]
    y_lines: tuple[int, int, int, int, int]
    confidence: float

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (
            self.x_lines[0],
            self.y_lines[0],
            self.x_lines[-1],
            self.y_lines[-1],
        )


@dataclass(frozen=True, slots=True)
class ScanResult:
    board: Board
    grid: GridDetection
    confidence: float
    image_width: int
    image_height: int
    unknown_cells: tuple[tuple[int, int], ...] = ()


class RGBImage:
    def __init__(self, width: int, height: int, pixels: bytes) -> None:
        if len(pixels) != width * height * 3:
            raise ValueError("Dữ liệu RGB không khớp kích thước ảnh")
        self.width = width
        self.height = height
        self.pixels = pixels

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        offset = (y * self.width + x) * 3
        return tuple(self.pixels[offset : offset + 3])  # type: ignore[return-value]

    def feature(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        *,
        size: int = 12,
    ) -> tuple[float, ...]:
        left = max(0, min(self.width - 1, left))
        top = max(0, min(self.height - 1, top))
        right = max(left + 1, min(self.width, right))
        bottom = max(top + 1, min(self.height, bottom))
        values: list[float] = []
        for target_y in range(size):
            source_y = min(
                bottom - 1,
                top + int((target_y + 0.5) * (bottom - top) / size),
            )
            for target_x in range(size):
                source_x = min(
                    right - 1,
                    left + int((target_x + 0.5) * (right - left) / size),
                )
                red, green, blue = self.pixel(source_x, source_y)
                values.extend((red / 255.0, green / 255.0, blue / 255.0))
        return tuple(values)


def decode_png(data: bytes) -> RGBImage:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Ảnh quét 2048 không phải PNG")
    position = 8
    idat: list[bytes] = []
    width = height = bit_depth = color_type = interlace = -1
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filtering,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or filtering != 0:
                raise ValueError("PNG dùng kiểu nén/lọc không được hỗ trợ")
        elif chunk_type == b"IDAT":
            idat.append(payload)
        elif chunk_type == b"IEND":
            break
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError("Chỉ hỗ trợ PNG RGB/RGBA 8-bit không interlace")
    bytes_per_pixel = 3 if color_type == 2 else 4
    stride = width * bytes_per_pixel
    raw = zlib.decompress(b"".join(idat))
    previous = bytearray(stride)
    offset = 0
    rgb = bytearray(width * height * 3)
    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        for index in range(stride):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + above) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                row[index] = (row[index] + _paeth(left, above, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"PNG filter không được hỗ trợ: {filter_type}")
        for x in range(width):
            source = x * bytes_per_pixel
            target = (y * width + x) * 3
            if color_type == 6:
                alpha = row[source + 3] / 255.0
                rgb[target] = round(row[source] * alpha)
                rgb[target + 1] = round(row[source + 1] * alpha)
                rgb[target + 2] = round(row[source + 2] * alpha)
            else:
                rgb[target : target + 3] = row[source : source + 3]
        previous = row
    return RGBImage(width, height, bytes(rgb))


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distance_left = abs(estimate - left)
    distance_above = abs(estimate - above)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_above and distance_left <= distance_upper_left:
        return left
    return above if distance_above <= distance_upper_left else upper_left


def _board_line_pixel(red: int, green: int, blue: int) -> bool:
    return red > 60 and red > green * 1.25 and green < 145 and blue < 95


def _longest_true_run(values: Iterable[bool]) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest


def _line_candidates(scores: Sequence[int], minimum: int) -> list[tuple[int, int]]:
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    selected: list[tuple[int, int]] = []
    for position, score in ranked:
        if score < minimum:
            break
        if all(abs(position - existing) > 6 for existing, _score in selected):
            selected.append((position, score))
        if len(selected) >= 28:
            break
    return sorted(selected)


def _best_regular_five(
    candidates: Sequence[tuple[int, int]],
    *,
    extent: int,
    preferred_span: float | None = None,
) -> tuple[tuple[int, ...], float]:
    if len(candidates) < 5:
        raise RuntimeError("Không tìm đủ 5 đường lưới của bàn 2048")
    best: tuple[int, ...] | None = None
    best_quality = -math.inf
    count = len(candidates)
    for a in range(count - 4):
        for b in range(a + 1, count - 3):
            for c in range(b + 1, count - 2):
                for d in range(c + 1, count - 1):
                    for e in range(d + 1, count):
                        chosen = (candidates[a], candidates[b], candidates[c], candidates[d], candidates[e])
                        positions = tuple(item[0] for item in chosen)
                        # A full-width/full-height canvas border is often the
                        # strongest brown line in this game.  It cannot be a
                        # board divider: the real 4x4 popup is inset from the
                        # canvas on all sides.
                        if positions[0] <= 2 or positions[-1] >= extent - 3:
                            continue
                        if preferred_span is not None and any(
                            strength > preferred_span * 1.60
                            for _position, strength in chosen
                        ):
                            # Full-canvas decoration bars can be regularly
                            # spaced with the board lines, but are far longer
                            # than the square board itself.
                            continue
                        gaps = [positions[index + 1] - positions[index] for index in range(4)]
                        mean_gap = sum(gaps) / 4.0
                        if mean_gap < 25:
                            continue
                        deviation = sum(abs(gap - mean_gap) for gap in gaps) / (4.0 * mean_gap)
                        if deviation > 0.18:
                            continue
                        strength = sum(item[1] for item in chosen) / 5.0
                        quality = strength - deviation * strength * 2.2
                        if preferred_span is not None:
                            span_error = abs((positions[-1] - positions[0]) - preferred_span) / preferred_span
                            quality -= span_error * strength * 4.0
                        if quality > best_quality:
                            best = positions
                            best_quality = quality
    if best is None:
        raise RuntimeError("Các đường lưới 2048 không đều hoặc bàn chưa hiển thị đầy đủ")
    maximum_score = max(score for _position, score in candidates)
    return best, max(0.0, min(1.0, best_quality / max(1.0, maximum_score)))


def detect_grid(image: RGBImage) -> GridDetection:
    x_scores = [
        _longest_true_run(
            _board_line_pixel(*image.pixel(x, y)) for y in range(image.height)
        )
        for x in range(image.width)
    ]
    y_scores = [
        _longest_true_run(
            _board_line_pixel(*image.pixel(x, y)) for x in range(image.width)
        )
        for y in range(image.height)
    ]
    # The board is often a popup inside a much larger game canvas.  Requiring a
    # line to span most of the screenshot works for a cropped reference image,
    # but not for a live 1280x720 canvas.
    minimum_run = max(30, round(min(image.width, image.height) * 0.16))
    x_candidates = _line_candidates(x_scores, minimum_run)
    y_candidates = _line_candidates(y_scores, minimum_run)
    x_lines, x_confidence = _best_regular_five(x_candidates, extent=image.width)
    y_lines, y_confidence = _best_regular_five(
        y_candidates,
        extent=image.height,
        preferred_span=x_lines[-1] - x_lines[0],
    )
    x_gap = (x_lines[-1] - x_lines[0]) / 4.0
    y_gap = (y_lines[-1] - y_lines[0]) / 4.0
    aspect_error = abs(x_gap - y_gap) / max(x_gap, y_gap)
    if aspect_error > 0.22:
        raise RuntimeError("Vùng nhận diện không phải bàn 2048 vuông 4×4")
    return GridDetection(
        x_lines=tuple(x_lines),  # type: ignore[arg-type]
        y_lines=tuple(y_lines),  # type: ignore[arg-type]
        confidence=max(0.0, min(x_confidence, y_confidence) * (1.0 - aspect_error)),
    )


def region_feature(
    image: RGBImage,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> tuple[float, ...]:
    width = right - left
    height = bottom - top
    margin = 0.16
    left += round(width * margin)
    right -= round(width * margin)
    top += round(height * margin)
    bottom -= round(height * margin)

    # A colour distribution is much more stable than pixel templates when the
    # user runs the game at 500x300 instead of the 1280px reference capture.
    # Brown board pixels are ignored; the remaining histogram describes each
    # fruit sprite (orange, watermelon, banana, grape, ...).
    hue_bins = 18
    saturation_bins = 3
    value_bins = 3
    values = [0.0] * 30
    selected = 0
    total = max(1, (right - left) * (bottom - top))
    for y in range(top, bottom):
        for x in range(left, right):
            red, green, blue = image.pixel(x, y)
            maximum = max(red, green, blue)
            minimum = min(red, green, blue)
            saturation = (maximum - minimum) / max(1, maximum)
            if maximum < 100 or saturation < 0.18:
                continue
            hue, hsv_saturation, value = colorsys.rgb_to_hsv(
                red / 255.0,
                green / 255.0,
                blue / 255.0,
            )
            values[min(hue_bins - 1, int(hue * hue_bins))] += 1
            saturation_index = min(
                saturation_bins - 1,
                int(hsv_saturation * saturation_bins),
            )
            values[hue_bins + saturation_index] += 1
            values[
                hue_bins
                + saturation_bins
                + min(value_bins - 1, int(value * value_bins))
            ] += 1
            values[24] += red / 255.0
            values[25] += green / 255.0
            values[26] += blue / 255.0
            values[27] += float(red > green * 1.35)
            values[28] += float(blue > red * 0.75 and blue > green * 0.70)
            selected += 1
    if selected:
        values = [value / selected for value in values]
    values[29] = selected / total
    return tuple(values)


def region_spatial_feature(
    image: RGBImage,
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    grid_size: int = 4,
) -> tuple[float, ...]:
    """Describe where the bright parts of a fruit appear inside its cell.

    The colour histogram above is intentionally position independent.  That
    makes it stable across resolutions, but it can confuse similarly coloured
    fruit such as a yellow banana (level 3) and pineapple (level 9).  This
    compact 4x4 descriptor retains the sprite's silhouette and local colour
    while remaining independent of the source pixel dimensions.
    """
    width = right - left
    height = bottom - top
    margin = 0.10
    left += round(width * margin)
    right -= round(width * margin)
    top += round(height * margin)
    bottom -= round(height * margin)
    values: list[float] = []
    for grid_y in range(grid_size):
        block_top = top + (bottom - top) * grid_y // grid_size
        block_bottom = top + (bottom - top) * (grid_y + 1) // grid_size
        for grid_x in range(grid_size):
            block_left = left + (right - left) * grid_x // grid_size
            block_right = left + (right - left) * (grid_x + 1) // grid_size
            selected = 0
            red_total = green_total = blue_total = 0.0
            area = max(1, (block_right - block_left) * (block_bottom - block_top))
            for y in range(block_top, block_bottom):
                for x in range(block_left, block_right):
                    red, green, blue = image.pixel(x, y)
                    maximum = max(red, green, blue)
                    minimum = min(red, green, blue)
                    saturation = (maximum - minimum) / max(1, maximum)
                    # The cell background is dark red/brown.  Keeping only
                    # bright saturated pixels isolates the rendered fruit.
                    if maximum < 145 or saturation < 0.24:
                        continue
                    selected += 1
                    red_total += red / 255.0
                    green_total += green / 255.0
                    blue_total += blue / 255.0
            values.extend(
                (
                    selected / area,
                    red_total / max(1, selected),
                    green_total / max(1, selected),
                    blue_total / max(1, selected),
                )
            )
    return tuple(values)


def cell_feature(image: RGBImage, grid: GridDetection, row: int, column: int) -> tuple[float, ...]:
    return region_feature(
        image,
        grid.x_lines[column],
        grid.y_lines[row],
        grid.x_lines[column + 1],
        grid.y_lines[row + 1],
    )


def cell_spatial_feature(
    image: RGBImage,
    grid: GridDetection,
    row: int,
    column: int,
) -> tuple[float, ...]:
    return region_spatial_feature(
        image,
        grid.x_lines[column],
        grid.y_lines[row],
        grid.x_lines[column + 1],
        grid.y_lines[row + 1],
    )


def feature_distance(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second):
        raise ValueError("Hai vector tile không cùng kích thước")
    return sum((left - right) ** 2 for left, right in zip(first, second, strict=True)) / len(first)


REFERENCE_BOARD: Board = (
    (1, 0, 0, 1),
    (1, 2, 3, 0),
    (2, 3, 5, 6),
    (4, 6, 8, 7),
)


class TileVision:
    """Recognise tiles from local, versioned fruit references."""

    # Colour histograms are intentionally resolution independent, but two
    # similarly coloured sprites (notably mango level 8 and pineapple level 9)
    # can finish almost tied.  After a swipe, game rules tell us that every
    # non-empty tile in ``expected`` must keep its level.  Preserve that known
    # level only when its visual distance is still essentially tied with the
    # best candidate.  A clear visual winner is never overridden.
    EXPECTED_LEVEL_MARGIN = 0.00020
    # Empty cells are visually far from every fruit prototype (~0.04-0.08 in
    # the live 500x300 samples). This conservative boundary lets a confirmed
    # move carry inferred levels above the currently photographed set without
    # confusing a genuinely empty destination for a tile.
    OCCUPIED_EMPTY_DISTANCE = 0.020
    # Cross-validation across the high-resolution reference and same-size live
    # samples gives a narrow safe band around 0.03: enough to separate 3/9,
    # while colour remains authoritative for the similarly shaped 3/8 pair.
    SPATIAL_DISTANCE_WEIGHT = 0.03

    def __init__(self, reference_path: Path, *, match_threshold: float = 0.008) -> None:
        reference = decode_png(reference_path.read_bytes())
        reference_grid = detect_grid(reference)
        self.prototypes: dict[int, list[tuple[float, ...]]] = {}
        self.spatial_prototypes: dict[int, list[tuple[float, ...]]] = {}
        for row in range(4):
            for column in range(4):
                level = REFERENCE_BOARD[row][column]
                self.prototypes.setdefault(level, []).append(
                    cell_feature(reference, reference_grid, row, column)
                )
                self.spatial_prototypes.setdefault(level, []).append(
                    cell_spatial_feature(reference, reference_grid, row, column)
                )
        asset_dir = reference_path.parent
        for level_9_name in (
            "2048-level-9-live.png",
            "2048-level-9-live-alt.png",
        ):
            level_9_path = asset_dir / level_9_name
            if level_9_path.exists():
                level_9 = decode_png(level_9_path.read_bytes())
                self.prototypes.setdefault(9, []).append(
                    region_feature(level_9, 0, 0, level_9.width, level_9.height)
                )
                self.spatial_prototypes.setdefault(9, []).append(
                    region_spatial_feature(level_9, 0, 0, level_9.width, level_9.height)
                )
        combined_path = asset_dir / "2048-levels-10-12.png"
        if combined_path.exists():
            combined = decode_png(combined_path.read_bytes())
            self.prototypes.setdefault(10, []).append(
                region_feature(combined, 0, 0, 130, combined.height)
            )
            self.spatial_prototypes.setdefault(10, []).append(
                region_spatial_feature(combined, 0, 0, 130, combined.height)
            )
            self.prototypes.setdefault(12, []).append(
                region_feature(combined, 134, 0, combined.width, combined.height)
            )
            self.spatial_prototypes.setdefault(12, []).append(
                region_spatial_feature(combined, 134, 0, combined.width, combined.height)
            )
        level_11_path = asset_dir / "2048-level-11.png"
        if level_11_path.exists():
            level_11 = decode_png(level_11_path.read_bytes())
            self.prototypes.setdefault(11, []).append(
                region_feature(level_11, 0, 0, level_11.width, level_11.height)
            )
            self.spatial_prototypes.setdefault(11, []).append(
                region_spatial_feature(level_11, 0, 0, level_11.width, level_11.height)
            )
        level_11_live_path = asset_dir / "2048-level-11-live.png"
        if level_11_live_path.exists():
            level_11_live = decode_png(level_11_live_path.read_bytes())
            self.prototypes.setdefault(11, []).append(
                region_feature(
                    level_11_live,
                    0,
                    0,
                    level_11_live.width,
                    level_11_live.height,
                )
            )
            self.spatial_prototypes.setdefault(11, []).append(
                region_spatial_feature(
                    level_11_live,
                    0,
                    0,
                    level_11_live.width,
                    level_11_live.height,
                )
            )
        # Same-size live samples cover every fruit seen at the normal 500x300
        # viewport.  Multiple positions are kept for levels whose edge
        # resampling previously changed their colour histogram (3, 4 and 8).
        for live_path in sorted(asset_dir.glob("2048-live-level-*.png")):
            try:
                level = int(live_path.stem.split("-")[3])
            except (IndexError, ValueError):
                continue
            live = decode_png(live_path.read_bytes())
            self.prototypes.setdefault(level, []).append(
                region_feature(live, 0, 0, live.width, live.height)
            )
            self.spatial_prototypes.setdefault(level, []).append(
                region_spatial_feature(live, 0, 0, live.width, live.height)
            )
        self.match_threshold = float(match_threshold)

    def _level_distances(
        self,
        feature: Sequence[float],
        spatial: Sequence[float] | None = None,
    ) -> list[tuple[float, int]]:
        return sorted(
            (
                min(feature_distance(feature, prototype) for prototype in prototypes)
                + (
                    self.SPATIAL_DISTANCE_WEIGHT
                    * min(
                        feature_distance(spatial, prototype)
                        for prototype in self.spatial_prototypes[level]
                    )
                    if (
                        spatial is not None
                        and level != 0
                        and level in self.spatial_prototypes
                    )
                    else 0.0
                ),
                level,
            )
            for level, prototypes in self.prototypes.items()
        )

    def scan_png(
        self,
        png: bytes,
        *,
        expected: Board | None = None,
        trust_expected_levels: bool = False,
    ) -> ScanResult:
        image = decode_png(png)
        grid = detect_grid(image)
        features = [
            [cell_feature(image, grid, row, column) for column in range(4)]
            for row in range(4)
        ]
        spatial_features = [
            [cell_spatial_feature(image, grid, row, column) for column in range(4)]
            for row in range(4)
        ]
        board_rows = [[0] * 4 for _ in range(4)]
        confidences: list[float] = []
        unknown_cells: list[tuple[int, int]] = []
        for row in range(4):
            for column in range(4):
                feature = features[row][column]
                distances = self._level_distances(feature, spatial_features[row][column])
                distance, level = distances[0]
                expected_level = expected[row][column] if expected is not None else 0
                empty_distance = next(
                    candidate_distance
                    for candidate_distance, candidate_level in distances
                    if candidate_level == 0
                )
                if (
                    trust_expected_levels
                    and expected_level
                    and empty_distance >= self.OCCUPIED_EMPTY_DISTANCE
                ):
                    # ``expected`` was produced by the exact n+n -> n+1 move
                    # engine. Occupancy confirms that the tile arrived; its
                    # level is therefore more reliable than reclassifying the
                    # sprite, and can safely exceed the available prototypes.
                    board_rows[row][column] = expected_level
                    confidences.append(
                        max(0.60, min(1.0, empty_distance / 0.05))
                    )
                    continue
                if (
                    expected_level
                    and expected_level != level
                    and expected_level in self.prototypes
                ):
                    expected_distance = next(
                        candidate_distance
                        for candidate_distance, candidate_level in distances
                        if candidate_level == expected_level
                    )
                    if (
                        expected_distance <= self.match_threshold
                        and expected_distance - distance <= self.EXPECTED_LEVEL_MARGIN
                    ):
                        level = expected_level
                        distance = expected_distance
                if distance <= self.match_threshold:
                    board_rows[row][column] = level
                    next_distance = distances[1][0] if len(distances) > 1 else self.match_threshold
                    separation = max(0.0, min(1.0, (next_distance - distance) / 0.003))
                    confidences.append(
                        max(0.0, 1.0 - distance / self.match_threshold) * (0.55 + 0.45 * separation)
                    )
                else:
                    unknown_cells.append((row, column))
        confidence = grid.confidence * (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
        return ScanResult(
            board=tuple(tuple(row) for row in board_rows),  # type: ignore[arg-type]
            grid=grid,
            confidence=confidence,
            image_width=image.width,
            image_height=image.height,
            unknown_cells=tuple(unknown_cells),
        )


def _slide_left(line: Sequence[int]) -> tuple[tuple[int, int, int, int], int]:
    compact = [value for value in line if value]
    merged: list[int] = []
    score = 0
    index = 0
    while index < len(compact):
        value = compact[index]
        if index + 1 < len(compact) and compact[index + 1] == value:
            value += 1
            score += 2**value
            index += 2
        else:
            index += 1
        merged.append(value)
    merged.extend([0] * (4 - len(merged)))
    return tuple(merged), score  # type: ignore[return-value]


def move_board(board: Board, direction: str) -> MoveResult:
    if direction not in DIRECTIONS:
        raise ValueError(f"Hướng 2048 không hợp lệ: {direction}")
    score = 0
    if direction in ("left", "right"):
        rows: list[tuple[int, int, int, int]] = []
        for row in board:
            source = tuple(reversed(row)) if direction == "right" else row
            moved, gained = _slide_left(source)
            rows.append(tuple(reversed(moved)) if direction == "right" else moved)
            score += gained
        result = tuple(rows)
    else:
        columns: list[tuple[int, int, int, int]] = []
        for column in range(4):
            source = tuple(board[row][column] for row in range(4))
            if direction == "down":
                source = tuple(reversed(source))
            moved, gained = _slide_left(source)
            if direction == "down":
                moved = tuple(reversed(moved))  # type: ignore[assignment]
            columns.append(moved)
            score += gained
        result = tuple(
            tuple(columns[column][row] for column in range(4)) for row in range(4)
        )
    return MoveResult(result, result != board, score)


def available_moves(board: Board) -> tuple[str, ...]:
    return tuple(direction for direction in DIRECTIONS if move_board(board, direction).changed)


def is_valid_spawn_successor(expected: Board, observed: Board) -> bool:
    """Return whether observed is expected plus exactly one new level-1/2 tile."""
    spawned = 0
    for row in range(4):
        for column in range(4):
            before = expected[row][column]
            after = observed[row][column]
            if before:
                if after != before:
                    return False
                continue
            if after == 0:
                continue
            if after not in (1, 2):
                return False
            spawned += 1
    return spawned == 1


def evaluate_board(board: Board) -> float:
    empty = sum(value == 0 for row in board for value in row)
    maximum = max(value for row in board for value in row)
    merge_pairs = 0
    smoothness = 0.0
    for row in range(4):
        for column in range(4):
            value = board[row][column]
            if not value:
                continue
            for delta_row, delta_column in ((1, 0), (0, 1)):
                other_row, other_column = row + delta_row, column + delta_column
                if other_row >= 4 or other_column >= 4:
                    continue
                other = board[other_row][other_column]
                if other:
                    smoothness -= abs(value - other) ** 1.25
                    if value == other:
                        merge_pairs += 1
    snake_orders = (
        ((0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (1, 2), (1, 1), (1, 0), (2, 0), (2, 1), (2, 2), (2, 3), (3, 3), (3, 2), (3, 1), (3, 0)),
        ((0, 3), (0, 2), (0, 1), (0, 0), (1, 0), (1, 1), (1, 2), (1, 3), (2, 3), (2, 2), (2, 1), (2, 0), (3, 0), (3, 1), (3, 2), (3, 3)),
        ((3, 0), (3, 1), (3, 2), (3, 3), (2, 3), (2, 2), (2, 1), (2, 0), (1, 0), (1, 1), (1, 2), (1, 3), (0, 3), (0, 2), (0, 1), (0, 0)),
        ((3, 3), (3, 2), (3, 1), (3, 0), (2, 0), (2, 1), (2, 2), (2, 3), (1, 3), (1, 2), (1, 1), (1, 0), (0, 0), (0, 1), (0, 2), (0, 3)),
    )
    snake_score = max(
        sum((2 ** board[row][column]) * (0.86**index) for index, (row, column) in enumerate(order))
        for order in snake_orders
    )
    corners = (board[0][0], board[0][3], board[3][0], board[3][3])
    corner_bonus = 2**maximum if maximum in corners else 0
    return (
        empty * 850.0
        + merge_pairs * 180.0
        + smoothness * 34.0
        + snake_score * 5.0
        + corner_bonus * 7.0
        + (2**maximum) * 2.0
    )


class LegacySmart2048Solver:
    def __init__(self, *, time_budget_ms: int = 130, max_depth: int = 4) -> None:
        self.time_budget_ms = time_budget_ms
        self.max_depth = max_depth
        self._deadline = 0.0

    def choose_move(self, board: Board) -> tuple[str | None, float, int]:
        moves = available_moves(board)
        if not moves:
            return None, evaluate_board(board), 0
        self._deadline = time.perf_counter() + self.time_budget_ms / 1000.0
        best_move = moves[0]
        best_score = -math.inf
        completed_depth = 0
        for depth in range(1, self.max_depth + 1):
            if time.perf_counter() >= self._deadline:
                break
            depth_best_move = best_move
            depth_best_score = -math.inf
            try:
                for direction in moves:
                    moved = move_board(board, direction)
                    score = moved.score * 4.0 + self._chance_value(moved.board, depth - 1)
                    if score > depth_best_score:
                        depth_best_score = score
                        depth_best_move = direction
            except TimeoutError:
                break
            best_move, best_score = depth_best_move, depth_best_score
            completed_depth = depth
        return best_move, best_score, completed_depth

    def _check_time(self) -> None:
        if time.perf_counter() >= self._deadline:
            raise TimeoutError

    @lru_cache(maxsize=80_000)
    def _max_value(self, board: Board, depth: int) -> float:
        self._check_time()
        if depth <= 0:
            return evaluate_board(board)
        moves = available_moves(board)
        if not moves:
            return evaluate_board(board) - 50_000.0
        return max(
            move_board(board, direction).score * 4.0
            + self._chance_value(move_board(board, direction).board, depth - 1)
            for direction in moves
        )

    @lru_cache(maxsize=80_000)
    def _chance_value(self, board: Board, depth: int) -> float:
        self._check_time()
        empty = [(row, column) for row in range(4) for column in range(4) if board[row][column] == 0]
        if not empty:
            return self._max_value(board, depth)
        total = 0.0
        sample = empty
        if len(sample) > 8:
            step = len(sample) / 8.0
            sample = [sample[min(len(sample) - 1, int(index * step))] for index in range(8)]
        probability_per_cell = 1.0 / len(sample)
        for row, column in sample:
            for value, probability in ((1, 0.9), (2, 0.1)):
                rows = [list(source) for source in board]
                rows[row][column] = value
                spawned = tuple(tuple(source) for source in rows)
                total += probability_per_cell * probability * self._max_value(spawned, depth)
        return total


_ROW_LEFT_TABLE: list[int] | None = None
_ROW_RIGHT_TABLE: list[int] | None = None
_ROW_LEFT_SCORE_TABLE: list[int] | None = None
_ROW_RIGHT_SCORE_TABLE: list[int] | None = None
_ROW_HEURISTIC_TABLE: list[float] | None = None


def _reverse_packed_row(row: int) -> int:
    return (
        ((row >> 12) & 0x000F)
        | ((row >> 4) & 0x00F0)
        | ((row << 4) & 0x0F00)
        | ((row << 12) & 0xF000)
    )


def _packed_row(values: Sequence[int]) -> int:
    return sum((int(value) & 0xF) << (4 * index) for index, value in enumerate(values))


def _init_bitboard_tables() -> None:
    """Build the 65,536-row move/heuristic tables used by the fast solver."""
    global _ROW_HEURISTIC_TABLE
    global _ROW_LEFT_SCORE_TABLE
    global _ROW_LEFT_TABLE
    global _ROW_RIGHT_SCORE_TABLE
    global _ROW_RIGHT_TABLE
    if _ROW_LEFT_TABLE is not None:
        return
    left_table = [0] * 65_536
    right_table = [0] * 65_536
    left_scores = [0] * 65_536
    right_scores = [0] * 65_536
    heuristics = [0.0] * 65_536
    for packed in range(65_536):
        line = tuple((packed >> (4 * index)) & 0xF for index in range(4))
        moved_left, left_score = _slide_left(line)
        reversed_line = tuple(reversed(line))
        moved_reversed, right_score = _slide_left(reversed_line)
        left_table[packed] = _packed_row(moved_left)
        right_table[packed] = _packed_row(tuple(reversed(moved_reversed)))
        left_scores[packed] = left_score
        right_scores[packed] = right_score

        # Heuristic constants and row scoring are ported from nneonneo's
        # expectimax implementation. Levels in this mini game are already the
        # log2 ranks used by the original 2048 solver.
        rank_sum = sum(value**3.5 for value in line)
        empty = sum(value == 0 for value in line)
        merges = 0
        previous = run = 0
        for value in line:
            if value == 0:
                continue
            if value == previous:
                run += 1
            elif run:
                merges += 1 + run
                run = 0
            previous = value
        if run:
            merges += 1 + run
        monotonic_left = monotonic_right = 0.0
        for index in range(1, 4):
            previous_power = line[index - 1] ** 4
            current_power = line[index] ** 4
            if previous_power > current_power:
                monotonic_left += previous_power - current_power
            else:
                monotonic_right += current_power - previous_power
        heuristics[packed] = (
            200_000.0
            + 270.0 * empty
            + 700.0 * merges
            - 47.0 * min(monotonic_left, monotonic_right)
            - 11.0 * rank_sum
        )
    _ROW_LEFT_TABLE = left_table
    _ROW_RIGHT_TABLE = right_table
    _ROW_LEFT_SCORE_TABLE = left_scores
    _ROW_RIGHT_SCORE_TABLE = right_scores
    _ROW_HEURISTIC_TABLE = heuristics


def _board_to_bits(board: Board) -> int:
    bits = 0
    for row in range(4):
        for column in range(4):
            bits |= min(15, board[row][column]) << (4 * (row * 4 + column))
    return bits


def _transpose_bits(bits: int) -> int:
    a1 = bits & 0xF0F00F0FF0F00F0F
    a2 = bits & 0x0000F0F00000F0F0
    a3 = bits & 0x0F0F00000F0F0000
    a = a1 | (a2 << 12) | (a3 >> 12)
    b1 = a & 0xFF00FF0000FF00FF
    b2 = a & 0x00FF00FF00000000
    b3 = a & 0x00000000FF00FF00
    return b1 | (b2 >> 24) | (b3 << 24)


def _move_bits(bits: int, direction: str) -> tuple[int, int]:
    _init_bitboard_tables()
    assert _ROW_LEFT_TABLE is not None
    assert _ROW_LEFT_SCORE_TABLE is not None
    assert _ROW_RIGHT_TABLE is not None
    assert _ROW_RIGHT_SCORE_TABLE is not None
    vertical = direction in {"up", "down"}
    working = _transpose_bits(bits) if vertical else bits
    if direction in {"left", "up"}:
        table = _ROW_LEFT_TABLE
        scores = _ROW_LEFT_SCORE_TABLE
    else:
        table = _ROW_RIGHT_TABLE
        scores = _ROW_RIGHT_SCORE_TABLE
    moved = score = 0
    for row_index in range(4):
        packed = (working >> (row_index * 16)) & 0xFFFF
        moved |= table[packed] << (row_index * 16)
        score += scores[packed]
    if vertical:
        moved = _transpose_bits(moved)
    return moved, score


def _heuristic_bits(bits: int) -> float:
    _init_bitboard_tables()
    assert _ROW_HEURISTIC_TABLE is not None
    transposed = _transpose_bits(bits)
    return sum(
        _ROW_HEURISTIC_TABLE[(source >> shift) & 0xFFFF]
        for source in (bits, transposed)
        for shift in (0, 16, 32, 48)
    )


class Smart2048Solver:
    """Fast probability-aware expectimax using row lookup tables and caching."""

    def __init__(
        self,
        *,
        time_budget_ms: int = 300,
        max_depth: int = 7,
        probability_cutoff: float = 0.0001,
    ) -> None:
        self.time_budget_ms = time_budget_ms
        self.max_depth = max_depth
        self.probability_cutoff = probability_cutoff
        self._deadline = 0.0
        self._nodes = 0
        self._transposition: dict[tuple[int, int], float] = {}

    def choose_move(self, board: Board) -> tuple[str | None, float, int]:
        bits = _board_to_bits(board)
        legal = [
            (direction, moved)
            for direction in DIRECTIONS
            if (moved := _move_bits(bits, direction)[0]) != bits
        ]
        if not legal:
            return None, _heuristic_bits(bits), 0
        self._deadline = time.perf_counter() + self.time_budget_ms / 1000.0
        distinct = len({value for row in board for value in row if value})
        adaptive_depth = min(self.max_depth, max(3, distinct - 2))
        best_move = legal[0][0]
        best_score = -math.inf
        completed_depth = 0
        for depth in range(1, adaptive_depth + 1):
            if time.perf_counter() >= self._deadline:
                break
            self._nodes = 0
            self._transposition = {}
            depth_move = best_move
            depth_score = -math.inf
            try:
                for direction, moved in legal:
                    score = self._chance_value(moved, depth - 1, 1.0)
                    if score > depth_score:
                        depth_score = score
                        depth_move = direction
            except TimeoutError:
                break
            best_move, best_score = depth_move, depth_score
            completed_depth = depth
        return best_move, best_score, completed_depth

    def _check_time(self) -> None:
        self._nodes += 1
        if self._nodes & 0xFF == 0 and time.perf_counter() >= self._deadline:
            raise TimeoutError

    def _max_value(self, bits: int, depth: int, probability: float) -> float:
        self._check_time()
        if depth <= 0 or probability < self.probability_cutoff:
            return _heuristic_bits(bits)
        best = -math.inf
        for direction in DIRECTIONS:
            moved, _gained = _move_bits(bits, direction)
            if moved != bits:
                best = max(best, self._chance_value(moved, depth - 1, probability))
        return best if best > -math.inf else -1_000_000_000.0

    def _chance_value(self, bits: int, depth: int, probability: float) -> float:
        self._check_time()
        if probability < self.probability_cutoff:
            return _heuristic_bits(bits)
        key = (bits, depth)
        cached = self._transposition.get(key)
        if cached is not None:
            return cached
        empty_shifts = [shift for shift in range(0, 64, 4) if not (bits >> shift) & 0xF]
        if not empty_shifts:
            value = self._max_value(bits, depth, probability)
        else:
            cell_probability = probability / len(empty_shifts)
            total = 0.0
            for shift in empty_shifts:
                for level, spawn_probability in ((1, 0.9), (2, 0.1)):
                    spawned = bits | (level << shift)
                    if depth <= 0:
                        child = _heuristic_bits(spawned)
                    else:
                        child = self._max_value(
                            spawned,
                            depth,
                            cell_probability * spawn_probability,
                        )
                    total += spawn_probability * child
            value = total / len(empty_shifts)
        self._transposition[key] = value
        return value


def board_text(board: Board) -> str:
    return "/".join(
        ",".join(str(value) if value else "." for value in row) for row in board
    )


@dataclass(frozen=True, slots=True)
class Auto2048Decision:
    scan: ScanResult
    direction: str | None
    score: float
    depth: int
    waiting: bool = False


class Auto2048Player:
    """Stateful vision + expectimax planner for one Chrome profile."""

    def __init__(
        self,
        reference_path: Path | None = None,
        *,
        time_budget_ms: int = 300,
        max_depth: int = 7,
    ) -> None:
        reference_path = reference_path or Path(__file__).with_name("assets") / "2048-reference.png"
        self.vision = TileVision(reference_path)
        self.solver = Smart2048Solver(
            time_budget_ms=time_budget_ms,
            max_depth=max_depth,
        )
        self.expected: Board | None = None
        self.previous_board: Board | None = None
        self.pending_direction: str | None = None
        self.stale_retries = 0

    def plan(self, png: bytes) -> Auto2048Decision:
        scan = self.vision.scan_png(png)
        stale = (
            self.previous_board is not None
            and self.expected is not None
            and self.expected != self.previous_board
            and scan.board == self.previous_board
        )
        if stale:
            self.stale_retries += 1
            if self.stale_retries <= 3:
                # A WebGL frame can still show the previous board while the
                # move animation is settling. Never send the same gesture
                # again here: one solver decision must produce one touch only.
                return Auto2048Decision(scan, None, 0.0, 0, waiting=True)
            raise RuntimeError("Bàn 2048 chưa cập nhật sau 3 lần kiểm tra")
        self.stale_retries = 0
        if self.expected is not None:
            scan = self.vision.scan_png(
                png,
                expected=self.expected,
                trust_expected_levels=True,
            )
        if scan.unknown_cells:
            cells = ", ".join(f"r{row + 1}c{column + 1}" for row, column in scan.unknown_cells)
            raise RuntimeError(f"Chưa nhận dạng chắc chắn ô 2048: {cells}")
        if scan.confidence < 0.16:
            raise RuntimeError(
                f"Độ tin cậy nhận dạng bàn 2048 quá thấp ({scan.confidence:.0%})"
            )
        if self.expected is not None and not is_valid_spawn_successor(
            self.expected,
            scan.board,
        ):
            raise RuntimeError(
                "Bàn quan sát không khớp kết quả một lần vuốt; bỏ frame để tránh tăng level giả"
            )
        direction, score, depth = self.solver.choose_move(scan.board)
        self.previous_board = scan.board
        self.pending_direction = direction
        self.expected = (
            move_board(scan.board, direction).board if direction is not None else scan.board
        )
        return Auto2048Decision(scan, direction, score, depth)
