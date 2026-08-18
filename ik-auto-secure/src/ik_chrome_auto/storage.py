from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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


def write_retained_json(path: Path, value: Any, *, keep: int = 50) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    prune_files(path.parent, "*.json", keep=keep)
    return path
