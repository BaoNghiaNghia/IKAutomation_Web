"""Local-only Windows Agent core.

This module deliberately has no HTTP server and no ADB implementation.  It
defines the safety-critical lifecycle used by a future LDPlayer adapter: one
active lease per device, cooperative cancellation, atomic checkpoints and
monotonic events.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ik_chrome_auto.web_control import DeviceRunState


@dataclass(frozen=True, slots=True)
class DeviceConfiguration:
    width: int = 1280
    height: int = 720
    dpi: int = 240
    local_adb_enabled: bool = True

    def is_compliant(self) -> bool:
        return self.width == 1280 and self.height == 720 and self.dpi == 240 and self.local_adb_enabled


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    device_id: str
    instance_name: str
    configuration: DeviceConfiguration
    online: bool = True


class DeviceAdapter(Protocol):
    """Infrastructure boundary. A production adapter may wrap ldconsole/ADB."""

    def discover(self) -> tuple[DiscoveredDevice, ...]: ...

    def configure(self, device_id: str, configuration: DeviceConfiguration) -> None: ...

    def health_check(self, device_id: str) -> bool: ...


class FakeDeviceAdapter:
    """Deterministic adapter for CI and local Agent contract tests."""

    def __init__(self, devices: tuple[DiscoveredDevice, ...]) -> None:
        self.devices = {device.device_id: device for device in devices}
        self.configure_calls: list[str] = []

    def discover(self) -> tuple[DiscoveredDevice, ...]:
        return tuple(self.devices.values())

    def configure(self, device_id: str, configuration: DeviceConfiguration) -> None:
        device = self.devices[device_id]
        self.devices[device_id] = DiscoveredDevice(
            device.device_id, device.instance_name, configuration, device.online
        )
        self.configure_calls.append(device_id)

    def health_check(self, device_id: str) -> bool:
        return self.devices[device_id].online


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
class AgentEvent:
    event_id: str
    agent_id: str
    run_id: str
    device_id: str
    sequence: int
    event_type: str
    state: DeviceRunState
    occurred_at: datetime
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    run_id: str
    device_id: str
    profile_id: str
    state: DeviceRunState
    sequence: int
    updated_at: str


class AtomicCheckpointStore:
    """Writes same-directory temporary files then atomically replaces them."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, checkpoint: Checkpoint) -> Path:
        target = self.root / checkpoint.device_id / f"{checkpoint.profile_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        encoded = json.dumps(asdict(checkpoint), ensure_ascii=False, sort_keys=True).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    def load(self, device_id: str, profile_id: str) -> Checkpoint | None:
        target = self.root / device_id / f"{profile_id}.json"
        if not target.exists():
            return None
        raw = json.loads(target.read_text(encoding="utf-8"))
        return Checkpoint(
            run_id=str(raw["run_id"]),
            device_id=str(raw["device_id"]),
            profile_id=str(raw["profile_id"]),
            state=DeviceRunState(raw["state"]),
            sequence=int(raw["sequence"]),
            updated_at=str(raw["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class ActiveLease:
    lease_id: str
    device_id: str
    run_id: str
    generation: int
    expires_at: datetime


class DeviceLeaseRegistry:
    """Thread-safe per-device exclusive lease registry with fencing generations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, ActiveLease] = {}
        self._generations: dict[str, int] = {}

    def acquire(self, device_id: str, run_id: str, now: datetime, *, ttl_seconds: int = 60) -> ActiveLease:
        with self._lock:
            current = self._leases.get(device_id)
            if current is not None and current.expires_at > now:
                raise RuntimeError(f"Device {device_id} is already leased")
            generation = self._generations.get(device_id, 0) + 1
            self._generations[device_id] = generation
            lease = ActiveLease(
                str(uuid4()), device_id, run_id, generation, now + timedelta(seconds=ttl_seconds)
            )
            self._leases[device_id] = lease
            return lease

    def release(self, lease: ActiveLease) -> bool:
        with self._lock:
            if self._leases.get(lease.device_id) != lease:
                return False
            del self._leases[lease.device_id]
            return True

    def active(self, device_id: str, now: datetime) -> ActiveLease | None:
        with self._lock:
            lease = self._leases.get(device_id)
            if lease is not None and lease.expires_at <= now:
                del self._leases[device_id]
                return None
            return lease


@dataclass(slots=True)
class DeviceRun:
    run_id: str
    device_id: str
    profile_id: str
    state: DeviceRunState = DeviceRunState.QUEUED
    sequence: int = 0
    lease: ActiveLease | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)


class AgentHost:
    """Owns local runs; callers publish returned events to the future broker."""

    def __init__(
        self,
        agent_id: str,
        adapter: DeviceAdapter,
        checkpoints: AtomicCheckpointStore,
        leases: DeviceLeaseRegistry | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.adapter = adapter
        self.checkpoints = checkpoints
        self.leases = leases or DeviceLeaseRegistry()
        self._runs: dict[tuple[str, str], DeviceRun] = {}
        self._handled_commands: set[str] = set()

    def discover(self) -> tuple[DiscoveredDevice, ...]:
        return self.adapter.discover()

    def configure_if_needed(self, device_id: str) -> bool:
        device = next((item for item in self.adapter.discover() if item.device_id == device_id), None)
        if device is None:
            raise KeyError(f"Unknown device: {device_id}")
        if device.configuration.is_compliant():
            return False
        self.adapter.configure(device_id, DeviceConfiguration())
        return True

    def start(self, command_id: str, run_id: str, device_id: str, profile_id: str) -> tuple[AgentEvent, ...]:
        if command_id in self._handled_commands:
            return ()
        if not self.adapter.health_check(device_id):
            raise RuntimeError(f"Device {device_id} is offline")
        key = (run_id, device_id)
        if key in self._runs:
            raise RuntimeError(f"Device {device_id} is already part of run {run_id}")
        self._handled_commands.add(command_id)
        run = DeviceRun(run_id, device_id, profile_id)
        self._runs[key] = run
        return (self._transition(run, DeviceRunState.PREFLIGHT, "agent accepted start"),)

    def acquire_gameplay_lease(self, run_id: str, device_id: str, now: datetime) -> AgentEvent:
        run = self._run(run_id, device_id)
        run.cancellation.throw_if_cancelled()
        if run.state != DeviceRunState.READY:
            raise RuntimeError("Gameplay lease chỉ được cấp từ state ready")
        run.lease = self.leases.acquire(device_id, run_id, now)
        return self._transition(run, DeviceRunState.RUNNING, "gameplay lease granted")

    def mark_ready(self, run_id: str, device_id: str) -> AgentEvent:
        run = self._run(run_id, device_id)
        if run.state != DeviceRunState.PREFLIGHT:
            raise RuntimeError("Device phải preflight trước khi ready")
        return self._transition(run, DeviceRunState.READY, "preflight passed")

    def stop(self, run_id: str, device_id: str) -> AgentEvent:
        run = self._run(run_id, device_id)
        run.cancellation.cancel()
        if run.lease is not None:
            self.leases.release(run.lease)
            run.lease = None
        if run.state == DeviceRunState.STOPPED:
            return self._event(run, "device.stopped", "already stopped")
        return self._transition(run, DeviceRunState.STOPPED, "cancellation acknowledged")

    def restore(self, device_id: str, profile_id: str) -> Checkpoint | None:
        """A restart restores counters only; a future runtime must preflight again."""
        checkpoint = self.checkpoints.load(device_id, profile_id)
        if checkpoint is None:
            return None
        return Checkpoint(
            checkpoint.run_id,
            checkpoint.device_id,
            checkpoint.profile_id,
            DeviceRunState.PREFLIGHT,
            checkpoint.sequence,
            checkpoint.updated_at,
        )

    def _run(self, run_id: str, device_id: str) -> DeviceRun:
        try:
            return self._runs[(run_id, device_id)]
        except KeyError as error:
            raise KeyError(f"Unknown device run: {run_id}/{device_id}") from error

    def _transition(self, run: DeviceRun, state: DeviceRunState, detail: str) -> AgentEvent:
        run.state = state
        event = self._event(run, "device.state_changed", detail)
        self.checkpoints.save(
            Checkpoint(run.run_id, run.device_id, run.profile_id, run.state, run.sequence, event.occurred_at.isoformat())
        )
        return event

    def _event(self, run: DeviceRun, event_type: str, detail: str) -> AgentEvent:
        run.sequence += 1
        return AgentEvent(
            str(uuid4()), self.agent_id, run.run_id, run.device_id, run.sequence,
            event_type, run.state, datetime.now(UTC), detail,
        )
