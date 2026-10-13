"""Non-invasive AutoFarm observer for a live packaged IK Auto instance.

Reads JSONL tails only; never imports Playwright, connects CDP, clicks or
changes Chrome configuration. Writes an allowlisted snapshot for CodeLocal.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROFILE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
EVENT = re.compile(r"^[a-zA-Z0-9_-]{1,90}$")
PROBLEM_EVENTS = frozenset({
    "retry", "farm_canvas_too_small", "farm_canvas_resize_error",
    "resource_area_navigation_blocked", "search_round_area_waiting",
    "resource_area_continent_map_waiting", "search_result_unverified",
    "resource_area_exhausted", "resource_area_pool_unavailable",
    "farm_capture_error", "dispatch_unverified", "search_no_result",
})
PROGRESS_EVENTS = frozenset({
    "dispatch_verified", "search_resource_fallback",
    "search_round_area_relocated", "post_dispatch_roster_scanned",
    "world_map_verified", "resource_tab_verified", "tap_resource_search",
    "tap_find_resource", "resource_area_changed", "started",
})
MAX_FILES = 100
MAX_TAIL_BYTES = 200_000


def _iso(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _safe(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _tail_json(path: Path, limit_bytes: int = MAX_TAIL_BYTES) -> list[dict[str, Any]]:
    with path.open("rb") as stream:
        end = stream.seek(0, 2)
        if end > limit_bytes:
            stream.seek(-limit_bytes, 2)
            stream.readline()
        else:
            stream.seek(0)
        data = stream.read(limit_bytes)
    result: list[dict[str, Any]] = []
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def observe(logs_dir: Path, *, now: float | None = None, window_seconds: int = 900) -> dict[str, Any]:
    """Return ONLY event labels, safe profile IDs, counters and timestamps."""
    timestamp = time.time() if now is None else now
    if not logs_dir.is_dir():
        return {"status": "logs_missing", "profiles": [], "anomalies": []}
    paths = sorted(
        (
            path for path in logs_dir.iterdir()
            if path.is_file() and path.name.startswith("farm-") and path.suffix == ".jsonl"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:MAX_FILES]
    profiles: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            rows = _tail_json(path)
        except OSError:
            continue
        for row in rows:
            event = _safe(row.get("event"), EVENT)
            pid = _safe(row.get("profile_id"), PROFILE_ID)
            at = _iso(row.get("timestamp"))
            if event is None or pid is None or at is None or not (timestamp - window_seconds <= at <= timestamp + 30):
                continue
            profile = profiles.setdefault(pid, {
                "profile_id": pid, "last_event": None, "last_event_at": None,
                "latest_epoch": 0.0, "recent_events": Counter(),
                "latest_started_at": None, "latest_stopped_at": None,
                "last_progress_at": None, "last_dispatch_at": None,
            })
            profile["recent_events"][event] += 1
            if at >= profile["latest_epoch"]:
                profile["last_event"] = event
                profile["last_event_at"] = datetime.fromtimestamp(at, UTC).isoformat()
                profile["latest_epoch"] = at
            if event in PROGRESS_EVENTS and (profile["last_progress_at"] is None or at > profile["last_progress_at"]):
                profile["last_progress_at"] = at
            if event == "dispatch_verified" and (profile["last_dispatch_at"] is None or at > profile["last_dispatch_at"]):
                profile["last_dispatch_at"] = at
            if event == "started" and (profile["latest_started_at"] is None or at > profile["latest_started_at"]):
                profile["latest_started_at"] = at
            if event in {"stopped", "finished"} and (profile["latest_stopped_at"] is None or at > profile["latest_stopped_at"]):
                profile["latest_stopped_at"] = at

    anomalies: list[dict[str, str | int]] = []
    final_profiles: list[dict[str, Any]] = []
    for pid, row in sorted(profiles.items()):
        started, stopped = row["latest_started_at"], row["latest_stopped_at"]
        # Long-running sessions often started before the 15-minute
        # observation window. An ongoing recent Farm event without a newer
        # stop counts as observed activity, but not proof of game success.
        since_last = round(timestamp - row["latest_epoch"])
        is_active = bool(
            (started is not None and (stopped is None or started > stopped))
            or (
                since_last <= 180
                and row["last_event"] not in {"stopped", "finished"}
                and (stopped is None or row["latest_epoch"] > stopped)
            )
        )
        problem_counts = {
            event: count for event, count in row["recent_events"].items()
            if event in PROBLEM_EVENTS
        }
        for event, count in problem_counts.items():
            if count >= 3 and event not in {"search_no_result"}:
                anomalies.append({"profile_id": pid, "kind": "repeated_" + event, "count": count})
        if is_active and since_last > 180:
            anomalies.append({"profile_id": pid, "kind": "farm_log_stale", "age_seconds": since_last})
        final_profiles.append({
            "profile_id": pid, "active_in_observed_window": is_active,
            "last_event": row["last_event"], "last_event_at": row["last_event_at"],
            "last_progress_at": (
                datetime.fromtimestamp(row["last_progress_at"], UTC).isoformat()
                if row["last_progress_at"] is not None else None
            ),
            "last_dispatch_at": (
                datetime.fromtimestamp(row["last_dispatch_at"], UTC).isoformat()
                if row["last_dispatch_at"] is not None else None
            ),
            "problems": dict(sorted(problem_counts.items())),
        })
    return {
        "generated_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
        "window_seconds": window_seconds,
        "farm_log_files_scanned": len(paths),
        "profiles_seen": len(final_profiles),
        "profiles_active_in_observed_window": sum(p["active_in_observed_window"] for p in final_profiles),
        "anomalies": sorted(anomalies, key=lambda a: (a["kind"], a["profile_id"])),
        "profiles": final_profiles,
        "safety": "read-only Chrome/CDP; local allowlisted log summary",
    }


def write_snapshot(logs_dir: Path, payload: dict[str, Any]) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    dest = logs_dir / "farm-observer-snapshot.json"
    temp = logs_dir / f".farm-observer-{os.getpid()}.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, dest)
    return dest


def system_health() -> dict[str, Any]:
    """Aggregate host resource pressure with Windows native APIs only."""
    try:
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("Windows memory query failed")
        proc = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"], check=True,
            capture_output=True, text=True, errors="replace", timeout=15,
        )
        counts: Counter[str] = Counter()
        for fields in csv.reader(io.StringIO(proc.stdout)):
            if fields:
                name = fields[0].strip().casefold()
                if name in {"chrome.exe", "ik auto.exe"}:
                    counts[name] += 1
        commit_limit = int(status.ullTotalPageFile)
        commit_free = int(status.ullAvailPageFile)
        commit_used_percent = round(
            100 * (1 - commit_free / commit_limit), 1
        ) if commit_limit > 0 else None
        return {
            "ram_used_percent": int(status.dwMemoryLoad),
            "ram_available_gib": round(status.ullAvailPhys / (1024**3), 2),
            "commit_limit_gib": round(commit_limit / (1024**3), 2),
            "commit_available_gib": round(commit_free / (1024**3), 2),
            "commit_used_percent": commit_used_percent,
            "commit_critical": bool(
                commit_limit > 0 and (
                    commit_free < 4 * (1024**3)
                    or commit_used_percent >= 90
                )
            ),
            "chrome_process_count": counts["chrome.exe"],
            "ik_auto_process_count": counts["ik auto.exe"],
            "note": "Chrome process count is NOT number of profiles",
        }
    except (AttributeError, OSError, ValueError, subprocess.SubprocessError):
        return {"metrics_available": False}

def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only IK Auto Farm observer")
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    while True:
        summary = observe(args.logs)
        summary["system_health"] = system_health()
        dest = write_snapshot(args.logs, summary)
        print(json.dumps({
            "snapshot": str(dest), "profiles_seen": summary.get("profiles_seen", 0),
            "anomaly_count": len(summary.get("anomalies", []))
        }, ensure_ascii=False), flush=True)
        if not args.watch:
            return 0
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())