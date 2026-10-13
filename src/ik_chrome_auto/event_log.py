from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# A single process/session ID lets support correlate worker, Sync, Farm,
# Monitor, update and crash files without persisting authentication secrets.
SESSION_RUN_ID = uuid.uuid4().hex[:12]

_WINDOWS_INVALID_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')



_SECRET_KEY = re.compile(
    r"password|passwd|secret|token|cookie|authorization|credential|api.?key|"
    r"recovery.?code|totp|two.?factor|private.?key",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTH_VALUE = re.compile(
    r"(?i)(bearer\s+)[a-z0-9_.~+/=-]+|"
    r"((?:password|token|secret|api[_-]?key)\s*[=:]\s*)[^\s,&]+"
)


def _safe_string(value: str) -> str:
    def safe_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group())
            return f"{parsed.scheme}://{parsed.hostname or 'redacted'}[path-redacted]"
        except ValueError:
            return "[url-redacted]"

    return _AUTH_VALUE.sub(
        lambda match: (match.group(1) or match.group(2) or "") + "[REDACTED]",
        _URL.sub(safe_url, value[:12000]),
    )


def _safe_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[depth-limit]"
    if isinstance(value, dict):
        return {
            str(key)[:120]: (
                "[REDACTED]"
                if _SECRET_KEY.search(str(key))
                else _safe_payload(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_payload(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return _safe_string(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    # Unknown objects might expose passwords through __str__ / __repr__.
    return f"[{type(value).__name__}]"


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
    def __init__(
        self, path: Path, *, max_bytes: int = 5_000_000,
        backups: int = 3, retention_days: int = 20,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prune_expired_backups()

    def _prune_expired_backups(self) -> None:
        """Remove only aged rotated backups, never the active log or profiles."""
        if self.retention_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        for index in range(1, self.backups + 1):
            backup = self.path.with_name(f"{self.path.name}.{index}")
            try:
                if backup.is_file() and datetime.fromtimestamp(
                    backup.stat().st_mtime, tz=UTC
                ) < cutoff:
                    backup.unlink()
            except OSError:
                # A locked backup must never block game automation.
                pass

    def write(self, event: str, payload: dict[str, Any]) -> None:
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **payload,
            "run_id": SESSION_RUN_ID,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
        }
        line = json.dumps(_safe_payload(row), ensure_ascii=False)
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