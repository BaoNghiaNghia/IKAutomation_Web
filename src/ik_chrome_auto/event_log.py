from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_WINDOWS_INVALID_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def profile_log_path(logs_dir: Path, prefix: str, profile_name: str, profile_id: str) -> Path:
    """Return a readable, Windows-safe log filename for a profile."""
    display_name = profile_name.replace("·", "-")
    # `*` is forbidden in Windows filenames. Full-width punctuation keeps the
    # masked label familiar to the user while remaining a valid filename.
    display_name = display_name.replace("*", "＊").replace("...", "…")
    display_name = _WINDOWS_INVALID_FILE_NAME.sub("-", display_name)
    display_name = " ".join(display_name.split()).strip(". ")
    display_name = display_name[:120].rstrip(". ") or profile_id
    return logs_dir / f"{prefix}-{display_name}.jsonl"


def migrate_legacy_profile_log(legacy_path: Path, target_path: Path, *, backups: int = 2) -> None:
    """Rename previous ID-based logs when the readable destination is unused."""
    if legacy_path == target_path:
        return

    for suffix in ("", *(f".{index}" for index in range(1, backups + 1))):
        source = legacy_path.with_name(legacy_path.name + suffix)
        target = target_path.with_name(target_path.name + suffix)
        if not source.exists() or target.exists():
            continue
        try:
            source.replace(target)
        except OSError:
            # A legacy process may still have the file open. New entries will
            # still use the readable filename once this worker starts.
            continue


class JsonLineLog:
    def __init__(self, path: Path, *, max_bytes: int = 5_000_000, backups: int = 3) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, payload: dict[str, Any]) -> None:
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **payload,
        }
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock:
            self._rotate_if_needed(len(line.encode("utf-8")) + 1)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self.max_bytes <= 0 or not self.path.exists():
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        for index in range(self.backups, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if not source.exists():
                continue
            if index == self.backups:
                source.unlink()
            else:
                source.replace(target)
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
