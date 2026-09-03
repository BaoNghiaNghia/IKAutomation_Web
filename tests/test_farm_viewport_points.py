import threading
from types import SimpleNamespace

from ik_chrome_auto.farm_vision import (
    DetectedGameState,
    FarmTemplateId,
    GameDetectionResult,
    TemplateEvidence,
)
from ik_chrome_auto.runner import (
    AUTOMATION_RENDERER_SIZE,
    AUTOMATION_RENDERER_WINDOW_SIZE,
    FARM_MINIMUM_CANVAS_SIZE,
    FARM_MAX_RECOVERY_ATTEMPTS,
    FARM_NO_READY_TEAM_RESCAN_SECONDS,
    FARM_RENDERER_IDLE_RELEASE_SECONDS,
    FARM_REFERENCE_ASPECT_RATIO,
    FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS,
    ProfileWorker,
)
from ik_chrome_auto.farm_workflow import FarmGameState, FarmStep, FarmWorkflow
from ik_chrome_auto.models import WorkerState


def test_farm_layout_fallbacks_use_relative_canvas_positions_at_16_by_9() -> None:
    """Fallback controls retain their 16:9 locations without desktop pixels."""
    assert FARM_REFERENCE_ASPECT_RATIO == 16 / 9
    assert ProfileWorker._resource_tab_layout_bounds((1280, 720)) == (164, 647, 92, 47)
    assert ProfileWorker._resource_button_layout_bounds("wood", (1280, 720)) == (459, 464, 122, 130)
    assert ProfileWorker._map_toggle_layout_bounds((1280, 720)) == (13, 621, 80, 90)
    assert ProfileWorker._city_to_world_map_layout_bounds((1280, 720)) == (13, 621, 80, 90)
    assert ProfileWorker._world_map_search_layout_bounds((1280, 720)) == (425, 552, 57, 57)
    assert ProfileWorker._search_target_checkbox_layout_bounds((1280, 720)) == (996, 494, 51, 47)


def test_area_relocation_closes_search_panel_and_opens_continent_map_directly() -> None:
    def frame(
        state: DetectedGameState,
        template: FarmTemplateId | None = None,
        bounds: tuple[int, int, int, int] = (10, 20, 30, 40),
    ) -> GameDetectionResult:
        evidence = () if template is None else (TemplateEvidence(template, True, 1.0, bounds),)
        return GameDetectionResult(state, evidence)

    class Session:
        def __init__(self) -> None:
            self.frames = [
                frame(DetectedGameState.RESOURCE_SEARCH_PANEL),
                frame(DetectedGameState.WORLD_MAP, FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON),
                frame(DetectedGameState.CONTINENT_MAP, FarmTemplateId.CONTINENT_MAP_PIN_BUTTON),
            ]
            self.escape_calls = 0

        def detect_farm_state(self):
            return self.frames.pop(0), {}, (1280, 720)

        def press_escape(self) -> None:
            self.escape_calls += 1

        def read_focused_numeric_farm_input(self, _bounds, _size):
            return None

    session = Session()
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = session
    worker.profile = SimpleNamespace(id="account-2")
    worker.stop_event = threading.Event()
    worker._farm = FarmWorkflow()
    worker._automation_renderer_locked = True
    worker._farm_run_id = "run"
    worker._farm_area_epoch = 0
    selector_calls = []
    worker._farm_area_selector = SimpleNamespace(
        next=lambda **kwargs: (
            selector_calls.append(kwargs)
            or SimpleNamespace(
                point=(650, 954),
                exhausted=False,
                attempt=1,
                max_attempts=3,
                city_levels=(7, 8),
            )
        )
    )
    logs = []
    taps = []
    worker._log_farm = lambda event, payload: logs.append((event, payload))
    worker._tap_farm_game_control = lambda bounds, size: (
        taps.append((bounds, size)) or "touch_canvas_template"
    )

    result = worker._try_resource_area_relocation("iron", 6)

    assert result == "unavailable"  # Coordinate inputs are deliberately unreadable in this test.
    assert session.escape_calls == 1
    assert len(selector_calls) == 1
    assert taps == [((10, 20, 30, 40), (1280, 720))]
    assert ("close_search_panel_for_area_navigation", {}) in logs
    assert any(
        event == "tap_continent_map_button" and payload["from_state"] == "world_map"
        for event, payload in logs
    )
    assert not any(event == "tap_world_map_return_to_city" for event, _payload in logs)


def test_area_relocation_resumed_on_city_clicks_only_continent_map() -> None:
    city_map = TemplateEvidence(
        FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON,
        True,
        1.0,
        (202, 550, 50, 48),
    )
    continent_pin = TemplateEvidence(
        FarmTemplateId.CONTINENT_MAP_PIN_BUTTON,
        True,
        1.0,
        (690, 20, 40, 40),
    )

    class Session:
        def __init__(self) -> None:
            self.frames = [
                GameDetectionResult(DetectedGameState.CITY, (city_map,)),
                GameDetectionResult(DetectedGameState.CONTINENT_MAP, (continent_pin,)),
            ]

        def detect_farm_state(self):
            return self.frames.pop(0), {}, (1280, 720)

        def read_focused_numeric_farm_input(self, _bounds, _size):
            return None

    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = Session()
    worker.profile = SimpleNamespace(id="account-2")
    worker.stop_event = threading.Event()
    worker._farm = FarmWorkflow()
    worker._automation_renderer_locked = True
    worker._farm_run_id = "run"
    worker._farm_area_epoch = 0
    worker._farm_area_selector = SimpleNamespace(
        next=lambda **_kwargs: SimpleNamespace(
            point=(650, 954),
            exhausted=False,
            attempt=1,
            max_attempts=3,
            city_levels=(7, 8),
        )
    )
    logs = []
    taps = []
    worker._log_farm = lambda event, payload: logs.append((event, payload))
    worker._tap_farm_game_control = lambda bounds, size: (
        taps.append((bounds, size)) or "touch_canvas_template"
    )

    result = worker._try_resource_area_relocation("iron", 6)

    assert result == "unavailable"
    assert taps == [((202, 550, 50, 48), (1280, 720))]
    assert not any(event == "tap_world_map_return_to_city" for event, _payload in logs)
    assert any(event == "tap_continent_map_button" for event, _payload in logs)


def test_missing_continent_map_icon_stays_in_relocation_instead_of_preflight() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._farm = FarmWorkflow()
    worker._farm_area_relocation_pending = None
    worker._farm_next_at = 0.0
    worker._log_farm = lambda *_args: None
    worker._publish = lambda *_args: None
    worker._retry_farm_or_stop = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("City relocation must not reset to preflight")
    )

    handled = worker._apply_resource_area_relocation_result(
        "map_button_waiting",
        "iron",
        7,
        reason="search_button_remained_visible",
    )

    assert handled is True
    assert worker._farm_area_relocation_pending == ("iron", 7)


def test_coordinate_input_failure_keeps_pending_point_and_does_not_reset_preflight() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._farm = FarmWorkflow()
    worker._farm_area_relocation_pending = None
    pending_point = object()
    worker._farm_area_pending_selection = pending_point
    worker._farm_next_at = 0.0
    logs = []
    updates = []
    worker._log_farm = lambda event, payload: logs.append((event, payload))
    worker._publish = lambda state, message, detail="": updates.append((state, message, detail))
    worker._retry_farm_or_stop = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Coordinate retry must not reset the farm workflow")
    )

    handled = worker._apply_resource_area_relocation_result(
        "unavailable",
        "stone",
        6,
        reason="original_coordinates_unreadable",
    )

    assert handled is True
    assert worker._farm_area_relocation_pending == ("stone", 6)
    assert worker._farm_area_pending_selection is pending_point
    assert "thử lại bước X/Y" in updates[-1][1]


def test_new_farm_cycle_preserves_the_running_sessions_coordinate_bag() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    selector = object()
    worker._farm_area_selector = selector
    worker._farm_run_id = "account-2-running-session"
    worker._farm_area_epoch = 2
    worker._farm_recovery_attempts = 0

    worker._reset_farm_cycle()

    assert worker._farm_area_selector is selector
    assert worker._farm_run_id == "account-2-running-session"
    assert worker._farm_area_epoch == 2


def test_farm_layout_fallbacks_scale_for_a_compact_five_profile_viewport() -> None:
    """Each profile uses its own captured canvas rather than screen geometry."""
    assert ProfileWorker._resource_tab_layout_bounds((384, 216)) == (41, 184, 44, 32)
    assert ProfileWorker._resource_button_layout_bounds("wood", (384, 216)) == (138, 139, 36, 39)
    assert ProfileWorker._city_to_world_map_layout_bounds((384, 216)) == (0, 152, 60, 64)
    assert ProfileWorker._world_map_search_layout_bounds((384, 216)) == (117, 156, 38, 36)
    assert ProfileWorker._search_target_checkbox_layout_bounds((384, 216)) == (297, 145, 20, 20)


def test_farm_map_toggle_uses_exact_canvas_percentage_and_excludes_mail() -> None:
    left, top, width, height = ProfileWorker._map_toggle_layout_bounds((1280, 720))

    assert (left + width // 2, top + height // 2) == (53, 666)
    # Mail is around (151, 583) on the same 1280x720 City canvas.
    assert not (left <= 151 < left + width and top <= 583 < top + height)


def test_farm_map_toggle_prefers_background_compositor_ratio() -> None:
    class Session:
        def __init__(self) -> None:
            self.background_ratios: list[tuple[float, float]] = []
            self.synthetic_ratios: list[tuple[float, float]] = []
            self.touches: list[object] = []

        def dispatch_game_surface_mouse_ratio(self, x: float, y: float) -> None:
            self.background_ratios.append((x, y))

        def click_game_surface_ratio(self, x: float, y: float) -> None:
            self.synthetic_ratios.append((x, y))

        def tap_farm_template(self, *args: object) -> None:
            self.touches.append(args)

    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = Session()
    worker._automation_renderer_locked = True
    worker._automation_renderer_hold_until = 0.0
    bounds = worker._map_toggle_layout_bounds((1280, 720))

    worker._click_map_toggle(bounds, (1280, 720))

    assert worker.session.background_ratios == [(53 / 1280, 666 / 720)]
    assert worker.session.synthetic_ratios == []
    assert worker.session.touches == []
    assert worker._automation_renderer_hold_until > 0.0


def test_farm_panel_control_uses_mouse_only_after_720p_renderer_lock() -> None:
    class Session:
        def __init__(self) -> None:
            self.mouse: list[object] = []
            self.touches: list[object] = []

        def click_farm_template_mouse(self, bounds: object, image_size: object) -> None:
            self.mouse.append((bounds, image_size))

        def tap_farm_template(self, *args: object) -> None:
            self.touches.append(args)

    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = Session()
    worker._automation_renderer_locked = True
    bounds = worker._resource_tab_layout_bounds((1280, 720))

    method = worker._click_farm_panel_control(bounds, (1280, 720))

    assert method == "mouse_canvas_template"
    assert worker.session.mouse == [(bounds, (1280, 720))]
    assert worker.session.touches == []


def test_farm_game_control_prefers_touch_at_the_verified_canvas_bounds() -> None:
    class Session:
        def __init__(self) -> None:
            self.touches: list[object] = []

        def tap_farm_template(self, bounds: object, image_size: object) -> None:
            self.touches.append((bounds, image_size))

    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = Session()
    worker._automation_renderer_locked = True
    bounds = worker._world_map_search_layout_bounds((1280, 720))

    method = worker._tap_farm_game_control(bounds, (1280, 720))

    assert method == "touch_canvas_template"
    assert worker.session.touches == [(bounds, (1280, 720))]


def test_farm_first_search_attempt_uses_background_canvas_ratio() -> None:
    class Session:
        def __init__(self) -> None:
            self.ratios: list[tuple[float, float]] = []

        def dispatch_game_surface_mouse_ratio(self, x: float, y: float) -> None:
            self.ratios.append((x, y))

    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = Session()
    worker._automation_renderer_locked = True
    worker._automation_renderer_hold_until = 0.0
    bounds = worker._world_map_search_layout_bounds((1280, 720))

    method = worker._click_farm_background_game_control(bounds, (1280, 720))

    assert method == "cdp_canvas_ratio"
    assert worker.session.ratios == [(453.5 / 1280, 580.5 / 720)]
    assert worker._automation_renderer_hold_until > 0.0


def test_farm_keeps_720p_renderer_through_search_postcondition(monkeypatch) -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._automation_renderer_locked = True
    worker._automation_renderer_hold_until = 102.2
    worker._farm = FarmWorkflow()
    worker._farm_next_at = 102.0
    released: list[bool] = []
    worker._release_automation_renderer = lambda **_kwargs: released.append(True)
    monkeypatch.setattr("ik_chrome_auto.runner.time.monotonic", lambda: 100.0)

    worker._release_farm_renderer_when_idle()

    assert released == []


def test_farm_yields_720p_after_postcondition_even_for_a_short_next_poll(monkeypatch) -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._automation_renderer_locked = True
    worker._automation_renderer_hold_until = 99.0
    worker._farm = FarmWorkflow()
    worker._farm_next_at = 100.35
    released: list[bool] = []
    worker._release_automation_renderer = lambda **_kwargs: released.append(True)
    monkeypatch.setattr("ik_chrome_auto.runner.time.monotonic", lambda: 100.0)

    worker._release_farm_renderer_when_idle()

    assert released == [True]


def test_farm_rejects_tiny_renderer_captures_before_team_or_resource_input() -> None:
    """A tiny canvas cannot safely distinguish Ready from Busy labels."""
    assert AUTOMATION_RENDERER_SIZE == (1280, 720)
    assert AUTOMATION_RENDERER_WINDOW_SIZE == (1280, 720)
    assert FARM_MINIMUM_CANVAS_SIZE == (1280, 720)
    assert ProfileWorker._farm_canvas_is_usable((1280, 720)) is True
    assert ProfileWorker._farm_canvas_is_usable((640, 360)) is False
    assert ProfileWorker._farm_canvas_is_usable((366, 168)) is False
    assert ProfileWorker._farm_canvas_is_usable((186, 66)) is False


def test_farm_allows_a_slow_world_map_portal_transition() -> None:
    """A blank loading canvas is normal and must not fail after eight seconds."""
    assert FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS >= 30.0


def test_farm_can_yield_the_high_resolution_renderer_during_completed_waits() -> None:
    """Long completed waits still yield the one shared renderer."""
    assert FARM_RENDERER_IDLE_RELEASE_SECONDS < 1.0


def test_no_ready_team_wait_is_two_minutes_before_the_next_scan() -> None:
    assert FARM_NO_READY_TEAM_RESCAN_SECONDS == 120.0

    worker = ProfileWorker.__new__(ProfileWorker)
    worker._farm = FarmWorkflow()
    decision = worker._farm.decide(FarmGameState.WORLD_MAP, ready_teams=())

    assert decision.step == FarmStep.WAITING
    assert worker._farm.waiting_for_ready_team is True
    assert worker._world_map_decision_delay(decision, open_search_delay=0.35) == 120.0


def test_ready_team_keeps_the_short_world_map_follow_up() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._farm = FarmWorkflow()
    decision = worker._farm.decide(FarmGameState.WORLD_MAP, ready_teams=(2,))

    assert decision.step == FarmStep.OPEN_SEARCH
    assert worker._world_map_decision_delay(decision, open_search_delay=0.35) == 0.35


def test_farm_yields_720p_renderer_during_long_postcondition_wait(monkeypatch) -> None:
    """A later tick reacquires 720p, so a long settle cannot starve peers."""
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._automation_renderer_locked = True
    worker._automation_renderer_hold_until = 0.0
    worker._farm = FarmWorkflow()
    worker._farm.step = FarmStep.ENTER_WORLD_MAP
    worker._farm_next_at = 101.5
    worker._farm_city_clicks = 1
    released: list[bool] = []
    worker._release_automation_renderer = lambda **_kwargs: released.append(True)
    monkeypatch.setattr("ik_chrome_auto.runner.time.monotonic", lambda: 100.0)

    worker._release_farm_renderer_when_idle()

    assert released == [True]


def test_farm_restores_grid_after_waiting_cycle_is_complete(monkeypatch) -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._automation_renderer_locked = True
    worker._automation_renderer_hold_until = 0.0
    worker._farm = FarmWorkflow()
    worker._farm.step = FarmStep.WAITING
    worker._farm_next_at = 115.0
    worker._farm_city_clicks = 1
    released: list[bool] = []
    worker._release_automation_renderer = lambda **_kwargs: released.append(True)
    monkeypatch.setattr("ik_chrome_auto.runner.time.monotonic", lambda: 100.0)

    worker._release_farm_renderer_when_idle()

    assert released == [True]


def test_farm_retries_transient_failures_before_stopping(monkeypatch) -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker._farm_recovery_attempts = 0
    worker._farm = object()
    worker._farm_next_at = 0.0
    logs = []
    updates = []
    worker._log_farm = lambda event, payload: logs.append((event, payload))
    worker._publish = lambda state, message, detail="": updates.append((state, message, detail))
    worker._reset_farm_cycle = lambda **_kwargs: setattr(worker, "_farm", object())
    monkeypatch.setattr("ik_chrome_auto.runner.time.monotonic", lambda: 100.0)

    for attempt in range(1, FARM_MAX_RECOVERY_ATTEMPTS + 3):
        assert worker._retry_farm_or_stop("world_map", "World Map đang tải") is True
        assert logs[-1][0] == "retry"
        assert logs[-1][1]["attempt"] == attempt
        assert logs[-1][1]["continuous"] is True
        assert updates[-1][0] == WorkerState.RUNNING

    assert worker._farm is not None
    assert logs[-1][1]["backoff_step"] == FARM_MAX_RECOVERY_ATTEMPTS


def test_continent_coordinate_fields_use_canvas_ratio_offsets() -> None:
    # The pin itself is matched live. The two input fields are offset from it
    # by normalized canvas distances, which gives the same target for any
    # profile viewport.
    assert ProfileWorker._coordinate_fields_from_pin((400, 100, 40, 40), (1280, 720)) == (
        (276, 120, 2, 2),
        (359, 120, 2, 2),
    )
    assert ProfileWorker._coordinate_fields_from_pin((120, 50, 20, 20), (384, 216)) == (
        (87, 60, 2, 2),
        (112, 60, 2, 2),
    )
