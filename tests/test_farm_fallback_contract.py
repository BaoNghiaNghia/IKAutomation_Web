"""Verified Farm no-result fallback follows the browser implementation contract."""

from __future__ import annotations

from types import SimpleNamespace

from ik_chrome_auto.farm_workflow import FarmStep, FarmWorkflow
from ik_chrome_auto.runner import ProfileWorker


def _worker() -> tuple[ProfileWorker, list[tuple[str, dict]], list[tuple[str, int]]]:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.profile = SimpleNamespace(id="account-7")
    worker._farm = FarmWorkflow(resource_order=("wood", "iron", "food", "stone"))
    events: list[tuple[str, dict]] = []
    relocations: list[tuple[str, int]] = []
    worker._log_farm = lambda event, data: events.append((event, data))
    worker._publish = lambda *_args, **_kwargs: None
    worker._try_resource_area_relocation = lambda resource, level: (
        relocations.append((resource, level)) or "map_button_waiting"
    )
    return worker, events, relocations


def test_all_four_resources_are_tried_before_area_relocation() -> None:
    worker, events, relocations = _worker()
    for expected in ("iron", "food", "stone"):
        resource, level = worker._farm.current_target()
        assert worker._handle_search_no_result(
            resource, level, reason="toast:not_found", delay_seconds=2.5,
        )
        assert worker._farm.current_target() == (expected, 7)
        assert relocations == []
        assert worker._farm.step == FarmStep.OPEN_SEARCH
    resource, level = worker._farm.current_target()
    assert worker._handle_search_no_result(resource, level,
                                          reason="search_button_remained_visible",
                                          delay_seconds=1)
    assert relocations == [("stone", 7)]
    assert [event for event, _ in events].count("search_resource_fallback") == 3
    assert [event for event, _ in events].count("search_resource_round_exhausted") == 1


def test_successful_relocation_restores_resource_search_workflow() -> None:
    worker, events, _ = _worker()
    worker._farm.step = FarmStep.FIND_RESOURCE
    assert worker._apply_resource_area_relocation_result(
        "moved", "stone", 7, reason="toast:no_result"
    )
    assert worker._farm.step == FarmStep.OPEN_SEARCH
    assert any(e == "search_round_area_relocated" for e, _ in events)


def test_unverified_relocation_is_bounded_and_has_safe_cooldown() -> None:
    worker, events, _ = _worker()
    retries: list[str] = []
    worker._retry_farm_or_stop = lambda reason, *args, **kwargs: (
        retries.append(reason) or True
    )
    worker._save_farm_debug_capture = lambda *_args: None
    for n in range(1, 7):
        assert worker._apply_resource_area_relocation_result(
            "unavailable", "stone", 7, reason="map_button_unverified"
        )
        if n < 6:
            assert worker._farm_area_wait_attempts == n
            assert retries == []
    assert retries == ["area_relocation_unverified"]
    assert any(e == "area_relocation_circuit_cooldown" for e, _ in events)