from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ik_chrome_auto.agent_core import (
    AgentHost,
    AtomicCheckpointStore,
    DeviceConfiguration,
    DeviceLeaseRegistry,
    DiscoveredDevice,
    FakeDeviceAdapter,
)
from ik_chrome_auto.web_control import DeviceRunState


def make_agent(tmp_path, *, configuration: DeviceConfiguration = DeviceConfiguration()) -> tuple[AgentHost, FakeDeviceAdapter]:
    adapter = FakeDeviceAdapter((DiscoveredDevice("device-1", "LDPlayer-1", configuration),))
    return AgentHost("agent-1", adapter, AtomicCheckpointStore(tmp_path / "checkpoints")), adapter


def test_discovery_only_reconfigures_a_non_compliant_device(tmp_path) -> None:
    agent, adapter = make_agent(tmp_path, configuration=DeviceConfiguration(dpi=120))

    assert agent.configure_if_needed("device-1")
    assert adapter.configure_calls == ["device-1"]
    assert not agent.configure_if_needed("device-1")


def test_start_is_idempotent_and_checkpointed(tmp_path) -> None:
    agent, _adapter = make_agent(tmp_path)

    events = agent.start("command-1", "run-1", "device-1", "profile-1")

    assert [event.state for event in events] == [DeviceRunState.PREFLIGHT]
    assert agent.start("command-1", "run-1", "device-1", "profile-1") == ()
    checkpoint = agent.checkpoints.load("device-1", "profile-1")
    assert checkpoint is not None and checkpoint.state == DeviceRunState.PREFLIGHT


def test_stop_releases_lease_and_restores_to_preflight_after_restart(tmp_path) -> None:
    agent, _adapter = make_agent(tmp_path)
    agent.start("command-1", "run-1", "device-1", "profile-1")
    agent.mark_ready("run-1", "device-1")
    agent.acquire_gameplay_lease("run-1", "device-1", datetime.now(UTC))

    stopped = agent.stop("run-1", "device-1")

    assert stopped.state == DeviceRunState.STOPPED
    assert agent.leases.active("device-1", datetime.now(UTC)) is None
    restored = agent.restore("device-1", "profile-1")
    assert restored is not None and restored.state == DeviceRunState.PREFLIGHT


def test_lease_is_exclusive(tmp_path) -> None:
    registry = DeviceLeaseRegistry()
    now = datetime.now(UTC)
    registry.acquire("device-1", "run-1", now)

    with pytest.raises(RuntimeError, match="already leased"):
        registry.acquire("device-1", "run-2", now)
