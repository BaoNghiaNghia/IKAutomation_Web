"""Bounded, per-gesture acknowledgements from Chrome follower CDP workers.

ACK means the follower's CDP input call returned successfully; it is NOT proof
that the game's WebGL state consumed the event.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Delivery:
    event_type: str
    expected: set[str]
    pending: set[str]
    started: float
    succeeded: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    max_latency_ms: float = 0.0


class SyncAckTracker:
    """One summary per critical input, not 44 blocking log writes per click."""

    def __init__(
        self,
        log: Callable[[str, dict[str, Any]], None],
        *,
        timeout_seconds: float = 10.0,
        max_pending: int = 128,
    ) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, _Delivery] = OrderedDict()
        self._timeout = timeout_seconds
        self._max_pending = max_pending
        self.completed = 0
        self.incomplete = 0
        self.last_max_latency_ms = 0.0

    def _summary(self, delivery_id: str, item: _Delivery) -> dict[str, Any]:
        return {
            "sync_delivery_id": delivery_id,
            "type": item.event_type,
            "target_count": len(item.expected),
            "ack_success_count": len(item.succeeded),
            "ack_failed_profile_ids": sorted(item.failed),
            "ack_missing_profile_ids": sorted(item.pending),
            "max_latency_ms": round(item.max_latency_ms, 1),
            "cdp_ack_only": True,
        }

    def _collect_expired_locked(
        self, now: float, *, force: bool = False
    ) -> list[tuple[str, dict[str, Any]]]:
        reports = []
        while self._pending:
            first_id, first = next(iter(self._pending.items()))
            if not force and len(self._pending) <= self._max_pending and now - first.started < self._timeout:
                break
            self._pending.pop(first_id)
            self.incomplete += 1
            reports.append(("sync_input_ack_incomplete", self._summary(first_id, first)))
        return reports

    def _emit(self, reports: list[tuple[str, dict[str, Any]]]) -> None:
        for event, payload in reports:
            self._log(event, payload)

    def start(self, delivery_id: str, event_type: str, profile_ids: set[str]) -> None:
        now = time.monotonic()
        with self._lock:
            reports = self._collect_expired_locked(now)
            self._pending[delivery_id] = _Delivery(event_type, set(profile_ids), set(profile_ids), now)
            reports.extend(self._collect_expired_locked(now))
        self._emit(reports)

    def ack(self, profile_id: str, delivery_id: str, success: bool, latency_ms: float) -> None:
        with self._lock:
            item = self._pending.get(delivery_id)
            if item is None or profile_id not in item.pending:
                return
            item.pending.remove(profile_id)
            (item.succeeded if success else item.failed).add(profile_id)
            item.max_latency_ms = max(item.max_latency_ms, latency_ms)
            reports = []
            if not item.pending:
                self._pending.pop(delivery_id)
                self.last_max_latency_ms = item.max_latency_ms
                if item.failed:
                    self.incomplete += 1
                    reports.append(("sync_input_ack_incomplete", self._summary(delivery_id, item)))
                else:
                    self.completed += 1
                    reports.append(("sync_input_ack_complete", self._summary(delivery_id, item)))
        self._emit(reports)

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            result = {
                "ack_completed_events": self.completed,
                "ack_incomplete_events": self.incomplete,
                "ack_pending_events": len(self._pending),
                "ack_last_max_latency_ms": round(self.last_max_latency_ms, 1),
            }
        return result

    def flush(self) -> None:
        with self._lock:
            reports = self._collect_expired_locked(time.monotonic(), force=True)
        self._emit(reports)