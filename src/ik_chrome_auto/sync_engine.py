"""Isolated manual mouse/keyboard synchronization engine.

This module deliberately knows nothing about AutoFarm, mailbox monitoring or
renderer leases.  It owns only manual-input membership, source arming and
fan-out, so changes to autonomous workflows cannot alter Sync behaviour.
"""
from __future__ import annotations

import threading
import time
from collections.abc import MutableMapping
from typing import Any, Protocol

from ik_chrome_auto.models import CommandKind, WorkerCommand


class SyncWorker(Protocol):
    session: object | None

    def submit(self, command: WorkerCommand) -> None: ...

    def submit_synced_input(self, event: dict[str, object]) -> None: ...


class SyncEventLog(Protocol):
    def write(self, event: str, payload: dict[str, object]) -> None: ...


class SyncInputEngine:
    """Own the complete manual Sync lifecycle independently of automation."""

    def __init__(
        self,
        workers: MutableMapping[str, SyncWorker],
        event_log: SyncEventLog,
    ) -> None:
        self._workers = workers
        self._event_log = event_log
        self._lock = threading.Lock()
        self.enabled = False
        self.master_id: str | None = None
        self.target_ids: set[str] = set()
        self._pending_pointer_down: dict[str, object] | None = None
        self._last_source_fingerprint: tuple[object, ...] | None = None
        self._last_source_at = 0.0

    def enable(self, master_id: str, target_ids: set[str] | None = None) -> None:
        if master_id not in self._workers:
            raise KeyError(f"Không tìm thấy profile master: {master_id}")
        targets = (
            {profile_id for profile_id in self._workers if profile_id != master_id}
            if target_ids is None
            else {
                profile_id
                for profile_id in target_ids
                if profile_id in self._workers and profile_id != master_id
            }
        )
        if not targets:
            raise ValueError("Hãy chọn ít nhất một profile nhận đồng bộ")
        with self._lock:
            previous_master = self.master_id if self.enabled else None
            self.enabled = True
            self.master_id = master_id
            self.target_ids = targets
            self._pending_pointer_down = None
            self._last_source_fingerprint = None
            self._last_source_at = 0.0
        self._event_log.write(
            "sync_enabled",
            {"master_profile_id": master_id, "target_profile_ids": sorted(targets)},
        )
        # Measuring coordinates consumes input by design. A Sync source must
        # explicitly leave that mode before its event probe is armed.
        self._workers[master_id].submit(
            WorkerCommand(CommandKind.SET_INSPECTOR, {"enabled": False})
        )
        self._event_log.write("sync_source_inspector_disabled", {"profile_id": master_id})
        prepared = 0
        for profile_id in sorted(targets):
            worker = self._workers.get(profile_id)
            if worker is None or worker.session is None:
                continue
            worker.submit(WorkerCommand(CommandKind.PREPARE_SYNC_TARGET, {}))
            prepared += 1
        self._event_log.write(
            "sync_targets_preparing",
            {"master_profile_id": master_id, "target_count": prepared},
        )
        if previous_master is not None and previous_master != master_id:
            previous = self._workers.get(previous_master)
            if previous is not None:
                previous.submit(WorkerCommand(CommandKind.SET_SYNC_SOURCE, {"enabled": False}))
        self._workers[master_id].submit(
            WorkerCommand(CommandKind.SET_SYNC_SOURCE, {"enabled": True})
        )

    def disable(self) -> None:
        with self._lock:
            was_enabled = self.enabled
            previous_master = self.master_id
            self.enabled = False
            self.master_id = None
            self.target_ids.clear()
            self._pending_pointer_down = None
            self._last_source_fingerprint = None
            self._last_source_at = 0.0
        if was_enabled:
            self._event_log.write("sync_disabled", {"master_profile_id": previous_master})
        worker = self._workers.get(previous_master or "")
        if worker is not None:
            worker.submit(WorkerCommand(CommandKind.SET_SYNC_SOURCE, {"enabled": False}))

    def add_target(self, profile_id: str) -> bool:
        with self._lock:
            if (
                not self.enabled
                or profile_id == self.master_id
                or profile_id not in self._workers
                or profile_id in self.target_ids
            ):
                return False
            self.target_ids.add(profile_id)
            master_id = self.master_id
            targets = sorted(self.target_ids)
        self._event_log.write(
            "sync_target_added",
            {"master_profile_id": master_id, "profile_id": profile_id, "target_profile_ids": targets},
        )
        return True

    def dispatch(self, source_profile_id: str, event: dict[str, object]) -> int:
        with self._lock:
            if not self.enabled or source_profile_id != self.master_id:
                return 0
            fingerprint = self._source_fingerprint(event)
            now = time.monotonic()
            # An open tab can briefly run its old and upgraded probes together.
            # Ignore only the immediate duplicate of the same native event.
            if (
                event.get("captured_at")
                and
                fingerprint == self._last_source_fingerprint
                and now - self._last_source_at < 0.08
            ):
                return 0
            self._last_source_fingerprint = fingerprint
            self._last_source_at = now
            target_ids = set(self.target_ids)
        event_type = str(event.get("type", ""))
        # A normal click generates pointerdown and pointerup only a few
        # milliseconds apart, while a 45-profile fan-out needs far longer.
        # Enqueueing them separately makes a follower's release arrive after
        # the game has discarded its stale press. Keep the pair together as a
        # single worker command. A move turns the pending press into a drag
        # and retains the ordinary pointer stream.
        pending = self._pending_pointer_down
        if event_type == "pointerdown":
            if pending is not None:
                self._dispatch_input(target_ids, pending)
            self._pending_pointer_down = dict(event)
            return 0
        if event_type == "pointerup" and pending is not None:
            self._pending_pointer_down = None
            delivered = self._dispatch_click(target_ids, pending, event)
            self._event_log.write(
                "sync_click_dispatched",
                {
                    "master_profile_id": source_profile_id,
                    "target_count": delivered,
                    "down_sequence": int(pending.get("sequence", 0) or 0),
                    "up_sequence": int(event.get("sequence", 0) or 0),
                },
            )
            return delivered
        if pending is not None:
            self._pending_pointer_down = None
            self._dispatch_input(target_ids, pending)
        delivered = self._dispatch_input(target_ids, event)
        if event_type in {"pointerdown", "pointerup", "keydown", "keyup"}:
            canvas = event.get("canvas")
            viewport = event.get("viewport")
            source = canvas if isinstance(canvas, dict) else viewport
            self._event_log.write(
                "sync_input_dispatched",
                {
                    "master_profile_id": source_profile_id,
                    "type": event_type,
                    "target_count": delivered,
                    "sequence": int(event.get("sequence", 0) or 0),
                    "source": {
                        key: source.get(key)
                        for key in ("ratio_x", "ratio_y", "css_x", "css_y", "css_width", "css_height", "backing_width", "backing_height")
                        if isinstance(source, dict) and key in source
                    },
                },
            )
        return delivered

    @staticmethod
    def _source_fingerprint(event: dict[str, object]) -> tuple[object, ...]:
        """Identify duplicate observations of one physical source event."""
        event_type = str(event.get("type", ""))
        canvas = event.get("canvas")
        viewport = event.get("viewport")
        point = canvas if isinstance(canvas, dict) else viewport if isinstance(viewport, dict) else {}
        keyboard = event.get("keyboard")
        key_data = keyboard if isinstance(keyboard, dict) else {}
        return (
            event_type,
            point.get("ratio_x"),
            point.get("ratio_y"),
            point.get("css_x"),
            point.get("css_y"),
            key_data.get("code"),
            key_data.get("key"),
        )

    def _dispatch_input(self, target_ids: set[str], event: dict[str, object]) -> int:
        """Queue one non-atomic Sync event to every available follower."""
        delivered = 0
        for profile_id in target_ids:
            worker = self._workers.get(profile_id)
            if worker is None or worker.session is None:
                continue
            submit_synced_input = getattr(worker, "submit_synced_input", None)
            if callable(submit_synced_input):
                submit_synced_input(event)
            else:
                worker.submit(WorkerCommand(CommandKind.SYNC_INPUT, {"event": event}))
            delivered += 1
        return delivered

    def _dispatch_click(
        self,
        target_ids: set[str],
        down: dict[str, object],
        up: dict[str, object],
    ) -> int:
        delivered = 0
        for profile_id in target_ids:
            worker = self._workers.get(profile_id)
            if worker is None or worker.session is None:
                continue
            worker.submit(
                WorkerCommand(CommandKind.SYNC_CLICK, {"down": down, "up": dict(up)})
            )
            delivered += 1
        return delivered
