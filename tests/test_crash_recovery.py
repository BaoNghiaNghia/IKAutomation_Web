"""Retained Chrome and bounded Playwright crash diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

from ik_chrome_auto.browser import ChromeProfileSession
from ik_chrome_auto.models import WorkerState
from ik_chrome_auto.runner import ProfileWorker


class TargetClosedError(RuntimeError):
    pass


def test_detach_never_closes_browser_even_if_ik_auto_owns_process() -> None:
    calls: list[str] = []
    session = ChromeProfileSession.__new__(ChromeProfileSession)
    session._owns_browser_process = True
    session._page = None
    session._context = SimpleNamespace(close=lambda: calls.append("closed_context"))
    session._playwright = SimpleNamespace(stop=lambda: calls.append("stopped_playwright"))
    session._detach_page_cdp_session = lambda: None
    session._configured_frames = set()
    session._tracked_pages = set()

    session.close(close_browser=False)

    assert "closed_context" not in calls
    assert "stopped_playwright" in calls
    assert session._context is None


def test_three_crashed_page_errors_detach_only_and_publish_actionable_error() -> None:
    events: list[tuple[str, dict]] = []
    close_flags: list[bool] = []
    statuses: list[tuple[WorkerState, str]] = []
    detached: list[tuple[str, str]] = []
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.profile = SimpleNamespace(id="account-42")
    worker.event_log = SimpleNamespace(write=lambda e, v: events.append((e, v)))
    worker.session = object()
    worker.on_detached = lambda pid, reason: detached.append((pid, reason))
    worker._publish = lambda state, message: statuses.append((state, message))

    def detach(*, close_browser: bool) -> None:
        close_flags.append(close_browser)
        worker.session = None

    worker._close_session = detach
    for _ in range(3):
        worker._record_pump_error(TargetClosedError(": Page crashed"))

    assert close_flags == [False]
    assert detached == [("account-42", "page_crashed")]
    assert statuses[0][0] == WorkerState.ERROR
    assert "Chrome được giữ nguyên" in statuses[0][1]
    assert [name for name, _ in events].count("worker_pump_error") == 1
    assert [name for name, _ in events].count("profile_renderer_crashed") == 1


def test_identical_transient_pump_errors_do_not_flood_disk() -> None:
    events: list[tuple[str, dict]] = []
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.profile = SimpleNamespace(id="account-42")
    worker.event_log = SimpleNamespace(write=lambda e, v: events.append((e, v)))
    worker.session = object()

    for _ in range(100):
        worker._record_pump_error(RuntimeError("temporary failure"))

    assert [e for e, _ in events] == ["worker_pump_error"]
    assert worker._pump_failure_count == 100

    worker._clear_pump_error()
    assert events[-1][0] == "worker_pump_recovered"
    assert events[-1][1]["previous_consecutive_errors"] == 100
