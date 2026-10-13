"""Read-only support summary of IK Auto local JSONL logs.

Returns allowlisted technical metadata, never raw messages, screenshots,
passwords, cookies, auth headers, clipboard or account export contents.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ik_chrome_auto.config import load_config

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_LOG_SUFFIXES = (".jsonl", ".jsonl.1", ".jsonl.2", ".jsonl.3")


def _safe_identifier(value: Any) -> str:
    text = str(value)
    return text if _SAFE_IDENTIFIER.fullmatch(text) else "[redacted]"


def _category(path: Path) -> str:
    if path.name.startswith("farm-"):
        return "farm"
    if path.name.startswith("coordinates-"):
        return "coordinates"
    for known in ("events", "lifecycle", "updater", "connection-health"):
        if path.name.startswith(known + "."):
            return known
    return "other"


def _tail_rows(path: Path, max_bytes: int) -> list[dict[str, Any]]:
    with path.open("rb") as handle:
        length = handle.seek(0, 2)
        if length > max_bytes:
            handle.seek(-max_bytes, 2)
            handle.readline()  # Drop the truncated first line.
        else:
            handle.seek(0)
        raw = handle.read(max_bytes)
    result: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            result.append(entry)
    return result


def summarize_logs(
    logs_dir: Path, *, max_files: int = 24, tail_bytes: int = 200_000
) -> dict[str, Any]:
    """Process bounded log tails, newest files first, without copying payloads."""
    candidates = []
    if logs_dir.is_dir():
        for path in logs_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix != ".jsonl" and not any(
                path.name.endswith(suffix) for suffix in _LOG_SUFFIXES[1:]
            ):
                continue
            candidates.append(path)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    events: Counter[str] = Counter()
    error_events: Counter[str] = Counter()
    affected_profiles: set[str] = set()
    categories: Counter[str] = Counter()
    latest = ""
    total_bytes = 0
    examined = 0
    for path in candidates[:max_files]:
        try:
            size = path.stat().st_size
            rows = _tail_rows(path, max_bytes=max(4096, tail_bytes))
        except OSError:
            continue
        examined += 1
        total_bytes += size
        categories[_category(path)] += 1
        for row in rows:
            name = _safe_identifier(row.get("event", "unknown"))
            events[name] += 1
            stamp = row.get("timestamp")
            if isinstance(stamp, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T[\d:.-]+(?:\+\d\d:\d\d|Z)", stamp
            ) and stamp > latest:
                latest = stamp
            if any(marker in name.lower() for marker in (
                "error", "fail", "timeout", "quarantin", "exception", "disconnect"
            )):
                error_events[name] += 1
                profile_id = row.get("profile_id")
                if profile_id is not None:
                    affected_profiles.add(_safe_identifier(profile_id))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "logs_found": len(candidates),
        "logs_examined": examined,
        "examined_total_bytes": total_bytes,
        "categories": dict(sorted(categories.items())),
        "events_by_type": dict(events.most_common(80)),
        "failures_by_type": dict(error_events.most_common(50)),
        "affected_profile_ids": sorted(affected_profiles)[:50],
        "latest_event_timestamp": latest or None,
        "read_only": True,
        "contains_raw_messages": False,
    }


def run_diagnostic_summary(config_path: Path) -> int:
    config = load_config(config_path)
    logs_dir = config.data_dir / "logs"
    report = summarize_logs(logs_dir)
    report["configured_enabled_profiles"] = sum(p.enabled for p in config.profiles)
    logs_dir.mkdir(parents=True, exist_ok=True)
    dest = logs_dir / "support-summary.json"
    # Never save any original event payload, Chrome data or screenshots.
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
