"""Non-invasive Windows Chrome/CDP health snapshot (no Playwright, no input)."""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ik_chrome_auto import browser
from ik_chrome_auto.config import load_config
from ik_chrome_auto.event_log import JsonLineLog
from ik_chrome_auto.models import AppConfig, ProfileMode


def collect_connection_health(config: AppConfig) -> dict[str, Any]:
    """Only read process/port metadata. Never launch, attach, resize or close Chrome."""
    start = time.monotonic()
    # One global process scan allows the same moved-port fallback as startup
    # reconnect without a separate machine-wide scan for each of 45 accounts.
    managed_ports = browser._live_managed_cdp_ports()
    def probe_profile(profile: Any) -> dict[str, Any]:
        started = time.monotonic()
        row: dict[str, Any] = {"profile_id": profile.id, "mode": profile.mode.value}
        try:
            if profile.mode != ProfileMode.MANAGED:
                row.update(state="external_cdp_not_verified", reason="no_local_process_identity")
            else:
                probe = browser.ChromeProfileSession(config, profile)
                expected = browser._normalized_windows_path(profile.user_data_dir.resolve())
                candidates = probe._managed_attach_candidates(
                    process_port=managed_ports.get(expected)
                )
                endpoint, results, selected_source = probe._select_managed_cdp_endpoint(
                    candidates
                )
                processes, roots, discovery = probe._matching_profile_chrome_processes()
                if endpoint is not None:
                    state = "verified_cdp"
                    reason = "identity_match"
                elif processes:
                    state = "chrome_without_verified_cdp"
                    reason = "endpoint_or_identity_unavailable"
                elif discovery != "ok":
                    state = "process_discovery_unavailable"
                    reason = discovery
                else:
                    state = "chrome_not_detected"
                    reason = "no_matching_process"
                row.update(
                    state=state,
                    reason=reason,
                    matched_process_count=len(processes),
                    root_process_count=len(roots),
                    selected_port=(
                        int(endpoint.rsplit(":", 1)[-1]) if endpoint else None
                    ),
                    port_source=selected_source,
                    candidate_count=len(results),
                    candidate_reasons=sorted({
                        str(candidate.get("reason", "")) for candidate in results
                    }),
                )
        except (OSError, RuntimeError, ValueError) as error:
            row.update(
                state="probe_error", reason=type(error).__name__,
            )
        row["duration_ms"] = round((time.monotonic() - started) * 1000)
        return row

    # The command is read-only; limit parallel socket checks so 45 stale
    # ports do not require 45 serial network timeouts on restart diagnostics.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cdp-health") as pool:
        rows = list(pool.map(probe_profile, (p for p in config.profiles if p.enabled)))
    counts = Counter(str(row["state"]) for row in rows)
    return {
        "configured_enabled": len(rows),
        "verified_cdp": counts.get("verified_cdp", 0),
        "states": dict(sorted(counts.items())),
        "duration_ms": round((time.monotonic() - start) * 1000),
        "profiles": rows,
    }


def run_connection_health(config_path: Path) -> int:
    config = load_config(config_path)
    report = collect_connection_health(config)
    log = JsonLineLog(config.data_dir / "logs" / "connection-health.jsonl")
    log.write("connection_health_snapshot", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Health is an observation, not a failing app startup. Return non-zero only
    # if the read-only diagnostic fails to execute (exceptions propagate).
    return 0