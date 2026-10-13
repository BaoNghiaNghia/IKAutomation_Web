"""Privacy-safe, bounded lifecycle diagnostics for desktop and worker exceptions."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import traceback
from functools import lru_cache
from pathlib import Path
from typing import Any

from ik_chrome_auto.event_log import SESSION_RUN_ID, JsonLineLog

_RUN_ID = SESSION_RUN_ID
_installed = False


@lru_cache(maxsize=8)
def diagnostic_log(config_path: Path) -> JsonLineLog:
    return JsonLineLog(
        config_path.parent / "data" / "logs" / "lifecycle.jsonl",
        max_bytes=5_000_000, backups=6, retention_days=20,
    )


def record(log: JsonLineLog, event: str, **payload: Any) -> None:
    """Keep diagnostics best-effort so logging never prevents automation."""
    with contextlib.suppress(OSError):
        log.write(event, {"run_id": _RUN_ID, "pid": os.getpid(), **payload})


def install_exception_hooks(log: JsonLineLog) -> None:
    global _installed
    if _installed:
        return
    _installed = True
    original_sys = sys.excepthook
    original_thread = threading.excepthook

    def on_unhandled(exc_type: type[BaseException], value: BaseException, tb: Any) -> None:
        record(
            log, "unhandled_exception", exception_type=exc_type.__name__,
            traceback="".join(traceback.format_exception(exc_type, value, tb))[-12000:],
        )
        original_sys(exc_type, value, tb)

    def on_thread(args: threading.ExceptHookArgs) -> None:
        record(
            log, "thread_exception", thread=args.thread.name if args.thread else "unknown",
            exception_type=args.exc_type.__name__ if args.exc_type else "unknown",
            traceback="".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )[-12000:],
        )
        original_thread(args)

    sys.excepthook = on_unhandled
    threading.excepthook = on_thread

def fleet_health_payload(dashboard: Any) -> dict[str, Any]:
    """A bounded, privacy-safe fleet snapshot without scanning Chrome windows.

    The dashboard's Qt thread calls this infrequently; only in-memory worker
    and queue state is read, so it never drives CDP or touches credentials.
    """
    runner = dashboard.runner
    profiles = [p for p in dashboard.config.profiles if p.enabled]
    ready = 0
    for profile in profiles:
        if runner.has_open_session(profile.id):
            ready += 1
    rows = getattr(dashboard, "rows", {})
    error_profiles = sorted(
        profile.id for profile in profiles
        if getattr(getattr(rows.get(profile.id), "last_state", None), "name", "") == "ERROR"
    )
    sync_metrics = runner.sync_metrics() if callable(getattr(runner, "sync_metrics", None)) else {}
    # Only aggregate ACK counters. Do not duplicate per-gesture/pointer input.
    ack = {
        key: sync_metrics[key] for key in (
            "ack_completed_events", "ack_incomplete_events",
            "ack_pending_events", "ack_last_max_latency_ms",
        ) if key in sync_metrics
    }
    return {
        "configured_enabled": len(profiles),
        "cdp_session_ready": ready,
        "error_count": len(error_profiles),
        "error_profile_ids": error_profiles[:50],
        # A bulk launch can be stuck even while Chrome keeps generating
        # heartbeat events. Include the scheduler's queue and exact worker
        # states so one STOPPED/STARTING profile is immediately visible.
        "bulk_open_phase": str(getattr(dashboard, "_farm_launcher_phase", "")),
        "bulk_open_queue_remaining": len(getattr(dashboard, "_farm_open_queue", ())),
        "commit_waiting": (
            getattr(dashboard, "_farm_launcher_phase", "") == "opening"
            and getattr(dashboard, "_farm_resource_pause_reason", None) == "commit_critical"
        ),
        "commit_wait_seconds": (
            max(0, int(time.monotonic() - dashboard._farm_resource_pause_started))
            if getattr(dashboard, "_farm_resource_pause_reason", None) == "commit_critical"
            and getattr(dashboard, "_farm_resource_pause_started", 0.0) else 0
        ),
        "commit_usage_percent": getattr(dashboard, "_farm_commit_snapshot_percent", None),
        "commit_available_gib": getattr(dashboard, "_farm_commit_snapshot_available_gib", None),
        "bulk_open_batch_ids": sorted(
            getattr(dashboard, "_farm_batch_profiles", ())
        )[:50],
        "bulk_open_batch_states": {
            profile_id: str(getattr(
                getattr(dashboard, "_farm_open_states", {}).get(profile_id),
                "value", "missing"
            ))
            for profile_id in sorted(getattr(dashboard, "_farm_batch_profiles", ()))[:50]
        },
        "reattach_pending_count": len(getattr(dashboard, "_reattach_pending_profiles", ())),
        "reattach_failed_count": len(getattr(dashboard, "_reattach_failed_profiles", ())),
        "farm_profile_count": len(getattr(dashboard, "farm_profiles", ())),
        "monitor_enabled": bool(getattr(dashboard, "_monitoring_enabled", False)),
        "monitor_in_flight_count": len(getattr(dashboard, "_monitor_in_flight", ())),
        "monitor_quarantined_ids": sorted(
            getattr(dashboard, "_monitor_quarantined_profiles", ())
        )[:50],
        "sync_active": bool(getattr(runner, "sync_enabled", False)),
        "ui_updates_pending": getattr(getattr(dashboard, "updates", None), "qsize", lambda: 0)(),
        **ack,
    }
