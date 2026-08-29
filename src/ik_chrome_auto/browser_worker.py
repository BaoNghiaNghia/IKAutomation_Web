"""Local browser-session lifecycle for managed Chrome profiles and CDP sessions."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ik_chrome_auto.browser_control import ProfileLease, ProfileRunState


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    browser_profile_id: str
    name: str
    mode: str
    healthy: bool = True


class BrowserSessionAdapter(Protocol):
    def discover_profiles(self) -> tuple[BrowserProfile, ...]: ...
    def open_profile(self, browser_profile_id: str) -> None: ...
    def health_check(self, browser_profile_id: str) -> bool: ...
    def reconnect(self, browser_profile_id: str) -> None: ...


class FakeBrowserSessionAdapter:
    def __init__(self, profiles: tuple[BrowserProfile, ...]) -> None:
        self.profiles = {profile.browser_profile_id: profile for profile in profiles}
        self.open_calls: list[str] = []
        self.reconnect_calls: list[str] = []

    def discover_profiles(self) -> tuple[BrowserProfile, ...]:
        return tuple(self.profiles.values())

    def open_profile(self, browser_profile_id: str) -> None:
        if not self.health_check(browser_profile_id):
            raise RuntimeError("Browser profile is unavailable")
        self.open_calls.append(browser_profile_id)

    def health_check(self, browser_profile_id: str) -> bool:
        return self.profiles[browser_profile_id].healthy

    def reconnect(self, browser_profile_id: str) -> None:
        self.reconnect_calls.append(browser_profile_id)


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def throw_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled("Operation cancelled")


class OperationCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserCheckpoint:
    run_id: str
    browser_profile_id: str
    farm_profile_id: str
    state: ProfileRunState
    sequence: int
    updated_at: str


class BrowserCheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, checkpoint: BrowserCheckpoint) -> Path:
        target = self.root / checkpoint.browser_profile_id / f"{checkpoint.farm_profile_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            handle.write(json.dumps(asdict(checkpoint), sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    def load(self, browser_profile_id: str, farm_profile_id: str) -> BrowserCheckpoint | None:
        target = self.root / browser_profile_id / f"{farm_profile_id}.json"
        if not target.exists():
            return None
        raw = json.loads(target.read_text(encoding="utf-8"))
        return BrowserCheckpoint(
            str(raw["run_id"]), str(raw["browser_profile_id"]), str(raw["farm_profile_id"]),
            ProfileRunState(raw["state"]), int(raw["sequence"]), str(raw["updated_at"]),
        )


class ProfileLeaseRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, ProfileLease] = {}
        self._generations: dict[str, int] = {}

    def acquire(self, worker_id: str, browser_profile_id: str, profile_run_id: str, now: datetime, *, ttl_seconds: int = 60) -> ProfileLease:
        with self._lock:
            current = self._leases.get(browser_profile_id)
            if current is not None and current.expires_at > now:
                raise RuntimeError(f"Browser profile {browser_profile_id} is already leased")
            generation = self._generations.get(browser_profile_id, 0) + 1
            self._generations[browser_profile_id] = generation
            lease = ProfileLease(str(uuid4()), worker_id, browser_profile_id, profile_run_id, generation, now + timedelta(seconds=ttl_seconds))
            self._leases[browser_profile_id] = lease
            return lease

    def release(self, lease: ProfileLease) -> bool:
        with self._lock:
            if self._leases.get(lease.browser_profile_id) != lease:
                return False
            del self._leases[lease.browser_profile_id]
            return True

    def active(self, browser_profile_id: str, now: datetime) -> ProfileLease | None:
        with self._lock:
            lease = self._leases.get(browser_profile_id)
            if lease is not None and lease.expires_at <= now:
                del self._leases[browser_profile_id]
                return None
            return lease


@dataclass(frozen=True, slots=True)
class BrowserWorkerEvent:
    event_id: str
    worker_id: str
    run_id: str
    browser_profile_id: str
    sequence: int
    state: ProfileRunState
    occurred_at: datetime
    detail: str


@dataclass(slots=True)
class ProfileRun:
    run_id: str
    browser_profile_id: str
    farm_profile_id: str
    state: ProfileRunState = ProfileRunState.QUEUED
    sequence: int = 0
    lease: ProfileLease | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)


class BrowserSessionWorker:
    def __init__(self, worker_id: str, adapter: BrowserSessionAdapter, checkpoints: BrowserCheckpointStore, leases: ProfileLeaseRegistry | None = None) -> None:
        self.worker_id = worker_id
        self.adapter = adapter
        self.checkpoints = checkpoints
        self.leases = leases or ProfileLeaseRegistry()
        self._runs: dict[tuple[str, str], ProfileRun] = {}
        self._handled_commands: set[str] = set()

    def discover_profiles(self) -> tuple[BrowserProfile, ...]:
        return self.adapter.discover_profiles()

    def start(self, command_id: str, run_id: str, browser_profile_id: str, farm_profile_id: str) -> tuple[BrowserWorkerEvent, ...]:
        if command_id in self._handled_commands:
            return ()
        if not self.adapter.health_check(browser_profile_id):
            raise RuntimeError(f"Browser profile {browser_profile_id} is offline")
        key = (run_id, browser_profile_id)
        if key in self._runs:
            raise RuntimeError("Browser profile is already part of this run")
        self.adapter.open_profile(browser_profile_id)
        self._handled_commands.add(command_id)
        run = ProfileRun(run_id, browser_profile_id, farm_profile_id)
        self._runs[key] = run
        return (self._transition(run, ProfileRunState.PREFLIGHT, "browser session opened"),)

    def mark_ready(self, run_id: str, browser_profile_id: str) -> BrowserWorkerEvent:
        run = self._run(run_id, browser_profile_id)
        if run.state != ProfileRunState.PREFLIGHT:
            raise RuntimeError("Profile phải preflight trước khi ready")
        return self._transition(run, ProfileRunState.READY, "browser preflight passed")

    def acquire_gameplay_lease(self, run_id: str, browser_profile_id: str, now: datetime) -> BrowserWorkerEvent:
        run = self._run(run_id, browser_profile_id)
        run.cancellation.throw_if_cancelled()
        if run.state != ProfileRunState.READY:
            raise RuntimeError("Profile lease chỉ được cấp từ state ready")
        run.lease = self.leases.acquire(self.worker_id, browser_profile_id, run_id, now)
        return self._transition(run, ProfileRunState.RUNNING, "profile lease granted")

    def stop(self, run_id: str, browser_profile_id: str) -> BrowserWorkerEvent:
        run = self._run(run_id, browser_profile_id)
        run.cancellation.cancel()
        if run.lease is not None:
            self.leases.release(run.lease)
            run.lease = None
        if run.state == ProfileRunState.STOPPED:
            return self._event(run, "already stopped")
        return self._transition(run, ProfileRunState.STOPPED, "cancellation acknowledged")

    def restore(self, browser_profile_id: str, farm_profile_id: str) -> BrowserCheckpoint | None:
        checkpoint = self.checkpoints.load(browser_profile_id, farm_profile_id)
        if checkpoint is None:
            return None
        return BrowserCheckpoint(checkpoint.run_id, checkpoint.browser_profile_id, checkpoint.farm_profile_id, ProfileRunState.PREFLIGHT, checkpoint.sequence, checkpoint.updated_at)

    def _run(self, run_id: str, browser_profile_id: str) -> ProfileRun:
        try:
            return self._runs[(run_id, browser_profile_id)]
        except KeyError as error:
            raise KeyError(f"Unknown browser profile run: {run_id}/{browser_profile_id}") from error

    def _transition(self, run: ProfileRun, state: ProfileRunState, detail: str) -> BrowserWorkerEvent:
        run.state = state
        event = self._event(run, detail)
        self.checkpoints.save(BrowserCheckpoint(run.run_id, run.browser_profile_id, run.farm_profile_id, run.state, run.sequence, event.occurred_at.isoformat()))
        return event

    def _event(self, run: ProfileRun, detail: str) -> BrowserWorkerEvent:
        run.sequence += 1
        return BrowserWorkerEvent(str(uuid4()), self.worker_id, run.run_id, run.browser_profile_id, run.sequence, run.state, datetime.now(UTC), detail)
