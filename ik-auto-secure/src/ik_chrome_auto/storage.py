from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ik_chrome_auto.game2048 import decode_png
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


def upscale_png_for_diagnostics(data: bytes, *, scale: int = 2) -> bytes:
    """Enlarge a PNG for local inspection without changing the live frame.

    Farm matching and click coordinates always use the original capture.  The
    retained copy is scaled with nearest-neighbour pixels only, so UI labels
    remain crisp when a compact Chrome profile produced a small canvas.
    """
    if scale < 1:
        raise ValueError("Tỷ lệ phóng ảnh phải lớn hơn hoặc bằng 1")
    if scale == 1:
        return data
    image = decode_png(data)
    source_stride = image.width * 3
    expanded = bytearray(image.width * scale * image.height * scale * 3)
    target_stride = image.width * scale * 3
    target_offset = 0
    for y in range(image.height):
        source_row = image.pixels[y * source_stride : (y + 1) * source_stride]
        enlarged_row = b"".join(
            source_row[offset : offset + 3] * scale
            for offset in range(0, source_stride, 3)
        )
        for _ in range(scale):
            expanded[target_offset : target_offset + target_stride] = enlarged_row
            target_offset += target_stride
    return encode_rgb_png(image.width * scale, image.height * scale, bytes(expanded))


def write_retained_json(path: Path, value: Any, *, keep: int = 50) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    prune_files(path.parent, "*.json", keep=keep)
    return path
