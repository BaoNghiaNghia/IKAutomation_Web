from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ik_chrome_auto.image_utils import decode_png
from ik_chrome_auto.windows import encode_rgb_png


def prune_profile_images(folder: Path, *, keep: int = 2) -> tuple[Path, ...]:
    """Keep only the newest PNG files in one profile screenshot folder."""
    if keep < 0:
        raise ValueError("keep không được âm")
    images = sorted(
        (path for path in folder.glob("*.png") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed: list[Path] = []
    for path in images[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
    return tuple(removed)


def prune_files(folder: Path, pattern: str, *, keep: int) -> tuple[Path, ...]:
    """Keep the newest matching files; snapshots may contain account data."""
    if keep < 0:
        raise ValueError("keep không được âm")
    files = sorted(
        (path for path in folder.glob(pattern) if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed: list[Path] = []
    for path in files[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
    return tuple(removed)


def write_retained_png(path: Path, data: bytes, *, keep: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    prune_profile_images(path.parent, keep=keep)
    return path


def upscale_png_for_diagnostics(
    data: bytes,
    *,
    width: int = 1280,
    height: int = 720,
    scale: int | None = None,
) -> bytes:
    """Create a fixed-size local debug image without changing live Chrome.

    Farm matching and click coordinates always use the original GDI capture.
    Only saved screenshots are expanded.  ``scale`` is retained for callers
    that explicitly need the legacy integer nearest-neighbour scaling.
    """
    if width < 1 or height < 1:
        raise ValueError("Kích thước ảnh debug phải lớn hơn 0")
    image = decode_png(data)
    if scale is not None:
        if scale < 1:
            raise ValueError("Tỷ lệ phóng ảnh phải lớn hơn hoặc bằng 1")
        width, height = image.width * scale, image.height * scale
    if (image.width, image.height) == (width, height):
        return data
    source_stride = image.width * 3
    target_stride = width * 3
    expanded = bytearray(target_stride * height)
    for target_y in range(height):
        source_y = min(image.height - 1, target_y * image.height // height)
        source_row = image.pixels[source_y * source_stride : (source_y + 1) * source_stride]
        target_offset = target_y * target_stride
        for target_x in range(width):
            source_x = min(image.width - 1, target_x * image.width // width)
            source_offset = source_x * 3
            expanded[target_offset + target_x * 3 : target_offset + target_x * 3 + 3] = source_row[
                source_offset : source_offset + 3
            ]
    return encode_rgb_png(width, height, bytes(expanded))


def write_retained_json(path: Path, value: Any, *, keep: int = 50) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    prune_files(path.parent, "*.json", keep=keep)
    return path
