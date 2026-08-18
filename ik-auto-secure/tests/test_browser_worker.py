from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ik_chrome_auto.browser_control import ProfileRunState
from ik_chrome_auto.browser_worker import (
    BrowserCheckpointStore,
    BrowserProfile,
    BrowserSessionWorker,
    FakeBrowserSessionAdapter,
    ProfileLeaseRegistry,
)


def make_worker(tmp_path) -> tuple[BrowserSessionWorker, FakeBrowserSessionAdapter]:
    adapter = FakeBrowserSessionAdapter((BrowserProfile("profile-1", "Main", "managed"),))
    return BrowserSessionWorker("worker-1", adapter, BrowserCheckpointStore(tmp_path / "checkpoints")), adapter


def test_start_is_idempotent_opens_profile_and_checkpoints(tmp_path) -> None:
    worker, adapter = make_worker(tmp_path)

    events = worker.start("command-1", "run-1", "profile-1", "farm-1")

    assert events[0].state == ProfileRunState.PREFLIGHT
    assert adapter.open_calls == ["profile-1"]
    assert worker.start("command-1", "run-1", "profile-1", "farm-1") == ()
    assert worker.checkpoints.load("profile-1", "farm-1") is not None


def test_stop_releases_profile_lease_and_restart_requires_preflight(tmp_path) -> None:
    worker, _adapter = make_worker(tmp_path)
    worker.start("command-1", "run-1", "profile-1", "farm-1")
    worker.mark_ready("run-1", "profile-1")
    worker.acquire_gameplay_lease("run-1", "profile-1", datetime.now(UTC))

    assert worker.stop("run-1", "profile-1").state == ProfileRunState.STOPPED
    assert worker.leases.active("profile-1", datetime.now(UTC)) is None
    assert worker.restore("profile-1", "farm-1").state == ProfileRunState.PREFLIGHT  # type: ignore[union-attr]


def test_profile_lease_is_exclusive(tmp_path) -> None:
    registry = ProfileLeaseRegistry()
    now = datetime.now(UTC)
    registry.acquire("worker-1", "profile-1", "run-1", now)

    with pytest.raises(RuntimeError, match="already leased"):
        registry.acquire("worker-1", "profile-1", "run-2", now)
