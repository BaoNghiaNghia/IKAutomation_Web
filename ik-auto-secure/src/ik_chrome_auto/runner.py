from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ik_chrome_auto.actions import ActionCancelled, AutomationFunctions
from ik_chrome_auto.browser import ChromeProfileSession
from ik_chrome_auto.event_log import JsonLineLog
from ik_chrome_auto.farm_vision import DetectedGameState, FarmTemplateId, TeamRosterRow
from ik_chrome_auto.farm_workflow import FarmGameState, FarmStep, FarmWorkflow
from ik_chrome_auto.models import (
    AppConfig,
    CommandKind,
    ProfileConfig,
    WorkerCommand,
    WorkerSnapshot,
    WorkerState,
)
from ik_chrome_auto.reader import redact
from ik_chrome_auto.resource_area_points import ResourceAreaPointSelector
from ik_chrome_auto.storage import upscale_png_for_diagnostics, write_retained_png
from ik_chrome_auto.windows import (
    calculate_tiled_positions,
    get_visible_window_rect,
    get_window_rect,
    get_window_process_tree_usage,
    get_work_area,
    move_window_outer,
    snapshot_process_parents,
    trim_window_process_tree,
    WindowRect,
)

UpdateCallback = Callable[[WorkerSnapshot], None]
InputCallback = Callable[[str, dict[str, object]], None]
CoordinateCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class ProfileResourceSnapshot:
    profile_id: str
    opened: bool
    process_count: int = 0
    ram_bytes: int = 0
    cpu_percent: float = 0.0


@dataclass(frozen=True, slots=True)
class ResourceOverview:
    total_profiles: int
    opened_profiles: int
    process_count: int
    ram_bytes: int
    cpu_percent: float
    profiles: tuple[ProfileResourceSnapshot, ...]


class ProfileWorker:
    def __init__(
        self,
        config: AppConfig,
        profile: ProfileConfig,
        event_log: JsonLineLog,
        on_update: UpdateCallback,
        on_input: InputCallback,
        on_coordinate: CoordinateCallback,
        *,
        drag_item_visible: bool = False,
        scrollbars_visible: bool = False,
        topmost: bool = False,
    ) -> None:
        self.config = config
        self.profile = profile
        self.event_log = event_log
        self.on_update = on_update
        self.on_input = on_input
        self.on_coordinate = on_coordinate
        self.coordinate_log = JsonLineLog(
            config.data_dir / "logs" / f"coordinates-{profile.id}.jsonl"
        )
        # Farm diagnostics stay separate from general dashboard events so a
        # failed template can be investigated without sifting through UI logs.
        self.farm_log = JsonLineLog(
            config.data_dir / "logs" / f"farm-{profile.id}.jsonl",
            max_bytes=2_000_000,
            backups=2,
        )
        self.stop_event = threading.Event()
        self.commands: queue.Queue[WorkerCommand] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.session: ChromeProfileSession | None = None
        self._thread_lock = threading.Lock()
        self._sync_source_enabled = False
        self._inspector_enabled = False
        self._drag_item_visible = drag_item_visible
        self._scrollbars_visible = scrollbars_visible
        self._topmost = topmost
        self._farm: FarmWorkflow | None = None
        self._farm_next_at = 0.0
        self._farm_city_clicks = 0
        self._farm_return_city_click_at = 0.0
        self._farm_return_city_clicks = 0
        self._farm_world_map_click_at = 0.0
        self._farm_ready_teams: tuple[int, ...] = ()
        self._farm_roster: tuple[TeamRosterRow, ...] = ()
        self._farm_search_clicks = 0
        self._farm_resource_tab_clicked_at = 0.0
        self._farm_resource_panel_verified = False
        self._farm_resource_template_misses = 0
        self._farm_resource_selected_at = 0.0
        self._farm_resource_selected_by_layout = False
        self._farm_target_checkbox_click_at = 0.0
        self._farm_target_checkbox_verified = False
        self._farm_target_checkbox_clicks = 0
        self._farm_find_resource_clicks = 0
        self._farm_find_resource_click_at = 0.0
        self._farm_gather_clicks = 0
        self._farm_capture_blocked_count = 0
        self._farm_team_selection_clicks = 0
        # The numbered badge is only used to derive the row to tap.  It can
        # resemble another badge after the row's artwork changes, so retain
        # the freshly resolved row as the authoritative post-tap target.
        self._farm_expected_team_row: tuple[int, int, int, int] | None = None
        self._farm_dispatch_click_at = 0.0
        self._farm_area_selector = ResourceAreaPointSelector()
        self._farm_area_epoch = 0
        self._farm_run_id = ""

    def submit(self, command: WorkerCommand) -> None:
        self._ensure_thread()
        self.commands.put(command)

    def stop(self) -> None:
        self.stop_event.set()
        self.submit(WorkerCommand(CommandKind.STOP))

    def shutdown(self) -> None:
        self.stop_event.set()
        self.submit(WorkerCommand(CommandKind.SHUTDOWN))

    def join(self, timeout: float = 5.0) -> None:
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout)

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(
                target=self._loop,
                name=f"chrome-profile-{self.profile.id}",
                daemon=True,
            )
            self.thread.start()

    def _publish(self, state: WorkerState, message: str, detail: str = "") -> None:
        snapshot = WorkerSnapshot(
            self.profile.id,
            state,
            message,
            detail,
            tuple((row.team, row.state.value) for row in self._farm_roster),
        )
        self.on_update(snapshot)
        self.event_log.write(
            "worker_status",
            {
                "profile_id": self.profile.id,
                "state": state.value,
                "message": message,
                "detail": detail,
            },
        )

    def _loop(self) -> None:
        while True:
            try:
                command = self.commands.get(timeout=0.04)
            except queue.Empty:
                if self.session is not None:
                    try:
                        self.session.pump(5)
                        self._poll_browser_events()
                    except Exception:
                        pass
                    if self.session is not None and not self.session.is_alive():
                        self._handle_external_close()
                    elif self._farm is not None and time.monotonic() >= self._farm_next_at:
                        self._run_farm_tick()
                continue
            try:
                if command.kind == CommandKind.SHUTDOWN:
                    self._close_session()
                    self._publish(WorkerState.STOPPED, "Đã đóng worker")
                    return
                if command.kind == CommandKind.STOP:
                    self._close_session()
                    self._publish(WorkerState.STOPPED, "Đã dừng profile")
                    continue
                if command.kind == CommandKind.START_FARM:
                    if self.session is None:
                        self._publish(WorkerState.STARTING, "Đang mở profile cho Auto Farm")
                        self._ensure_session(navigate=True)
                    self._farm = FarmWorkflow()
                    self._farm_next_at = 0.0
                    self._farm_city_clicks = 0
                    self._farm_return_city_click_at = 0.0
                    self._farm_return_city_clicks = 0
                    self._farm_world_map_click_at = 0.0
                    self._farm_ready_teams = ()
                    self._farm_roster = ()
                    self._farm_search_clicks = 0
                    self._farm_resource_tab_clicked_at = 0.0
                    self._farm_resource_panel_verified = False
                    self._farm_resource_template_misses = 0
                    self._farm_resource_selected_at = 0.0
                    self._farm_resource_selected_by_layout = False
                    self._farm_target_checkbox_click_at = 0.0
                    self._farm_target_checkbox_verified = False
                    self._farm_target_checkbox_clicks = 0
                    self._farm_find_resource_clicks = 0
                    self._farm_find_resource_click_at = 0.0
                    self._farm_gather_clicks = 0
                    self._farm_capture_blocked_count = 0
                    self._farm_team_selection_clicks = 0
                    self._farm_expected_team_row = None
                    self._farm_dispatch_click_at = 0.0
                    self._farm_area_selector = ResourceAreaPointSelector()
                    self._farm_area_epoch = 0
                    self._farm_run_id = f"{self.profile.id}-{time.monotonic_ns()}"
                    self._log_farm("started", {"resource_order": self._farm.resource_order})
                    self._publish(
                        WorkerState.RUNNING,
                        f"Auto Farm: đang preflight game canvas | thứ tự tài nguyên: {', '.join(self._farm.resource_order)}",
                    )
                    continue
                if command.kind == CommandKind.STOP_FARM:
                    self._farm = None
                    self._log_farm("stopped", {"reason": "user"})
                    self._publish(WorkerState.READY if self.session is not None else WorkerState.STOPPED, "Đã dừng Auto Farm")
                    continue
                if command.kind == CommandKind.SET_SYNC_SOURCE:
                    self._sync_source_enabled = bool(command.payload.get("enabled", False))
                    if self.session is not None:
                        self.session.set_sync_source(self._sync_source_enabled)
                    continue
                if command.kind == CommandKind.SET_INSPECTOR:
                    self._inspector_enabled = bool(command.payload.get("enabled", False))
                    if self.session is not None:
                        self.session.set_inspector(self._inspector_enabled)
                    self._publish(
                        WorkerState.READY,
                        "Đang đo tọa độ" if self._inspector_enabled else "Đã tắt đo tọa độ",
                    )
                    continue
                if command.kind == CommandKind.SET_DRAG_ITEM:
                    self._drag_item_visible = bool(command.payload.get("visible", True))
                    if self.session is not None:
                        self.session.set_drag_item_visible(self._drag_item_visible)
                    self._publish(
                        WorkerState.READY if self.session is not None else WorkerState.STOPPED,
                        (
                            "Đã hiện #drag-item"
                            if self._drag_item_visible
                            else "Đã ẩn #drag-item"
                        ),
                    )
                    continue
                if command.kind == CommandKind.SET_SCROLLBARS:
                    self._scrollbars_visible = bool(command.payload.get("visible", True))
                    if self.session is not None:
                        self.session.set_scrollbars_visible(self._scrollbars_visible)
                    self._publish(
                        WorkerState.READY if self.session is not None else WorkerState.STOPPED,
                        "Đã hiện thanh cuộn" if self._scrollbars_visible else "Đã ẩn thanh cuộn",
                    )
                    continue
                if command.kind == CommandKind.SET_TOPMOST:
                    self._topmost = bool(command.payload.get("enabled", False))
                    if self.session is not None:
                        self.session.set_topmost(self._topmost)
                    self._publish(
                        WorkerState.READY if self.session is not None else WorkerState.STOPPED,
                        "Đã ghim cửa sổ" if self._topmost else "Đã bỏ ghim cửa sổ",
                    )
                    continue
                if command.kind == CommandKind.MOVE_WINDOW:
                    if self.session is not None:
                        self.session.move_window(
                            int(command.payload["x"]),
                            int(command.payload["y"]),
                            topmost=self._topmost,
                        )
                        self._publish(WorkerState.READY, "Đã sắp xếp cửa sổ")
                    continue
                if command.kind == CommandKind.SYNC_INPUT:
                    if self.session is not None:
                        self.session.apply_synced_input(dict(command.payload["event"]))
                    continue
                if command.kind == CommandKind.RESIZE:
                    width = int(command.payload["width"])
                    height = int(command.payload["height"])
                    if self.session is not None:
                        self.session.resize(width, height)
                        self._publish(WorkerState.READY, f"Đã resize {width}×{height} px")
                    else:
                        self._publish(
                            WorkerState.STOPPED,
                            f"Sẽ resize {width}×{height} px khi mở profile",
                        )
                    continue
                self.stop_event.clear()
                if command.kind == CommandKind.OPEN:
                    self._publish(WorkerState.STARTING, "Đang mở Chrome profile")
                    self._ensure_session(navigate=True)
                    self._publish(WorkerState.READY, "Chrome và trang game đã mở")
                elif command.kind == CommandKind.READ:
                    self._publish(WorkerState.RUNNING, "Đang đọc dữ liệu")
                    functions = self._functions()
                    path = functions.read_state()
                    self._publish(WorkerState.COMPLETED, "Đã đọc dữ liệu", str(path))
                elif command.kind == CommandKind.SCREENSHOT:
                    self._publish(WorkerState.RUNNING, "Đang chụp ảnh")
                    path = self._functions().screenshot(str(command.payload.get("name", "manual")))
                    self._publish(WorkerState.COMPLETED, "Đã chụp ảnh", str(path))
            except ActionCancelled:
                self._publish(WorkerState.STOPPED, "Đã hủy action")
            except Exception as error:
                if self.session is not None and not self.session.is_alive():
                    self._handle_external_close()
                else:
                    detail = f"{type(error).__name__}: {error}"
                    self.event_log.write(
                        "worker_error",
                        {"profile_id": self.profile.id, "message": detail},
                    )
                    self._publish(WorkerState.ERROR, str(error), detail)
            finally:
                self.commands.task_done()

    def _ensure_session(self, *, navigate: bool) -> ChromeProfileSession:
        if self.session is None:
            self.session = ChromeProfileSession(self.config, self.profile)
            self.session.start(navigate=navigate)
            self.session.set_sync_source(self._sync_source_enabled)
            self.session.set_inspector(self._inspector_enabled)
            self.session.set_drag_item_visible(self._drag_item_visible)
            self.session.set_scrollbars_visible(self._scrollbars_visible)
            if self._topmost:
                self.session.set_topmost(True)
        elif navigate:
            self.session.goto()
        return self.session

    def _poll_browser_events(self) -> None:
        if self.session is None:
            return
        if self._sync_source_enabled:
            for event in self.session.poll_sync_events():
                self.on_input(self.profile.id, event)
        if self._inspector_enabled:
            for event in self.session.poll_coordinate_events():
                safe_event = redact(event)
                self.coordinate_log.write(
                    "coordinate",
                    {"profile_id": self.profile.id, "coordinate": safe_event},
                )
                self.on_coordinate(self.profile.id, safe_event)

    def _functions(self) -> AutomationFunctions:
        session = self._ensure_session(navigate=False)
        return AutomationFunctions(
            session,
            self.stop_event,
            lambda message: self._publish(WorkerState.RUNNING, message),
        )

    def _run_farm_tick(self) -> None:
        """Perform only the first fully template-verified City → World Map step.

        Further resource/team steps remain blocked until their browser-specific
        roster and action verifiers are ported from ADB.
        """
        if self.session is None or self._farm is None:
            return
        try:
            detected, _surface, image_size = self.session.detect_farm_state()
            self._farm_capture_blocked_count = 0
            state = {
                DetectedGameState.CITY: FarmGameState.CITY,
                DetectedGameState.WORLD_MAP: FarmGameState.WORLD_MAP,
                DetectedGameState.RESOURCE_SEARCH_PANEL: FarmGameState.RESOURCE_SEARCH,
                DetectedGameState.RESOURCE_POPUP: FarmGameState.RESOURCE_POPUP,
                DetectedGameState.TEAM_SELECTION: FarmGameState.TEAM_SELECTION,
                DetectedGameState.STORAGE_LIMIT_DIALOG: FarmGameState.STORAGE_LIMIT,
                DetectedGameState.RESOURCE_EXPIRY_DIALOG: FarmGameState.RESOURCE_EXPIRY,
            }.get(detected.state, FarmGameState.UNKNOWN)
            city = detected.evidence_for(FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
            map_to_city = detected.evidence_for(FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON)
            world = detected.evidence_for(FarmTemplateId.WORLD_MAP_ANCHOR)
            browser_canvas = detected.evidence_for(FarmTemplateId.BROWSER_CANVAS_READY_ANCHOR)
            world_map_back = detected.evidence_for(FarmTemplateId.BROWSER_WORLD_MAP_BACK_BUTTON)
            world_map_coordinate_pin = detected.evidence_for(FarmTemplateId.BROWSER_WORLD_MAP_COORDINATE_PIN)
            city_continent_map = detected.evidence_for(FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON)
            search_button = detected.evidence_for(FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON)
            resource_tab = detected.evidence_for(FarmTemplateId.BROWSER_RESOURCE_TAB_BUTTON)
            resource_buttons = {
                "food": detected.evidence_for(FarmTemplateId.BROWSER_FOOD_RESOURCE_BUTTON),
                "wood": detected.evidence_for(FarmTemplateId.BROWSER_WOOD_RESOURCE_BUTTON),
                "stone": detected.evidence_for(FarmTemplateId.BROWSER_STONE_RESOURCE_BUTTON),
                "iron": detected.evidence_for(FarmTemplateId.BROWSER_IRON_RESOURCE_BUTTON),
            }
            resource_active_buttons = {
                "food": detected.evidence_for(FarmTemplateId.BROWSER_FOOD_RESOURCE_ACTIVE),
                "wood": detected.evidence_for(FarmTemplateId.BROWSER_WOOD_RESOURCE_ACTIVE),
                "stone": detected.evidence_for(FarmTemplateId.BROWSER_STONE_RESOURCE_ACTIVE),
                "iron": detected.evidence_for(FarmTemplateId.BROWSER_IRON_RESOURCE_ACTIVE),
            }
            iron_resource = detected.evidence_for(FarmTemplateId.BROWSER_IRON_RESOURCE_BUTTON)
            target_checkbox_unchecked = detected.evidence_for(
                FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_UNCHECKED
            )
            find_resource = detected.evidence_for(FarmTemplateId.BROWSER_SEARCH_BUTTON_ENABLED)
            search_toasts = {
                "not_found": detected.evidence_for(FarmTemplateId.BROWSER_TOAST_NOT_FOUND),
                "not_found_short": detected.evidence_for(FarmTemplateId.BROWSER_TOAST_NOT_FOUND_SHORT),
                "other_region": detected.evidence_for(FarmTemplateId.BROWSER_TOAST_OTHER_REGION),
                "level_too_low": detected.evidence_for(FarmTemplateId.BROWSER_TOAST_LEVEL_TOO_LOW),
            }
            gather_button = detected.evidence_for(FarmTemplateId.BROWSER_GATHER_BUTTON_ENABLED)
            target_resource_expiry_toast = detected.evidence_for(
                FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_TOAST
            )
            target_resource_expiry_confirm = detected.evidence_for(
                FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_CONFIRM
            )
            team_panel = detected.evidence_for(FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL)
            team_action = detected.evidence_for(FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON)
            team_badges = {
                2: detected.evidence_for(FarmTemplateId.BROWSER_TEAM_2_BADGE),
                3: detected.evidence_for(FarmTemplateId.BROWSER_TEAM_3_BADGE),
                4: detected.evidence_for(FarmTemplateId.BROWSER_TEAM_4_BADGE),
            }
            selected_border = detected.evidence_for(FarmTemplateId.BROWSER_TEAM_SELECTED_BORDER)
            expected_team = self._farm.team
            expected_badge = team_badges.get(expected_team) if expected_team is not None else None
            expected_row_bounds = self._farm_expected_team_row
            if expected_row_bounds is None and expected_team is not None:
                expected_row_bounds = self._team_row_for_selection(
                    expected_team,
                    team_badges,
                    image_size,
                )
            expected_team_selected = self._is_expected_team_selected(
                expected_badge,
                selected_border,
                expected_row_bounds,
            )
            ready_teams = detected.ready_teams
            roster = detected.team_roster
            # The World Map transition hides the team HUD. Retain a roster
            # detected in the stable City frame and use it only for this farm
            # run; it is reset whenever Farm is started again.
            if ready_teams:
                self._farm_ready_teams = ready_teams
            if roster:
                self._farm_roster = roster
            self._log_farm(
                "detection",
                {
                    "state": detected.state.value,
                    "canvas": {"width": image_size[0], "height": image_size[1]},
                    "city": self._evidence_payload(city),
                    "map_to_city": self._evidence_payload(map_to_city),
                    "world_map": self._evidence_payload(world),
                    "world_map_back": self._evidence_payload(world_map_back),
                    "world_map_coordinate_pin": self._evidence_payload(world_map_coordinate_pin),
                    "city_continent_map": self._evidence_payload(city_continent_map),
                    "browser_canvas": self._evidence_payload(browser_canvas),
                    "resource_search_button": self._evidence_payload(search_button),
                    "resource_tab": self._evidence_payload(resource_tab),
                    "resource_buttons": {
                        resource: self._evidence_payload(button)
                        for resource, button in resource_buttons.items()
                    },
                    "resource_active_buttons": {
                        resource: self._evidence_payload(button)
                        for resource, button in resource_active_buttons.items()
                    },
                    "iron_resource": self._evidence_payload(iron_resource),
                    "target_checkbox_unchecked": self._evidence_payload(target_checkbox_unchecked),
                    "target_checkbox_verified": self._farm_target_checkbox_verified,
                    "find_resource": self._evidence_payload(find_resource),
                    "search_toasts": {
                        name: self._evidence_payload(toast) for name, toast in search_toasts.items()
                    },
                    "gather_button": self._evidence_payload(gather_button),
                    "target_resource_expiry_toast": self._evidence_payload(target_resource_expiry_toast),
                    "target_resource_expiry_confirm": self._evidence_payload(target_resource_expiry_confirm),
                    "team_panel": self._evidence_payload(team_panel),
                    "team_action": self._evidence_payload(team_action),
                    "team_badges": {
                        str(team): self._evidence_payload(badge)
                        for team, badge in team_badges.items()
                    },
                    "selected_border": self._evidence_payload(selected_border),
                    "expected_team_selected": expected_team_selected,
                    "dispatch_clicked": self._farm_dispatch_click_at > 0,
                    "roster": [
                        {"team": row.team, "state": row.state.value, "evidence": row.evidence}
                        for row in roster
                    ],
                    "ready_teams": ready_teams,
                },
            )
            # Ported from ADB ResourceSearchExecutionService: probe the short
            # toast window after each fresh Search tap. A verified negative
            # toast advances the already-fixed level/resource plan; it is not
            # a technical failure and must never cause a blind repeat tap.
            if self._farm_find_resource_click_at > 0:
                toast_elapsed = time.monotonic() - self._farm_find_resource_click_at
                matched_toast = next(
                    (name for name, toast in search_toasts.items() if toast.found),
                    None,
                )
                if matched_toast is not None:
                    resource, level = self._farm.current_target()
                    screenshot = self._save_farm_debug_capture(f"search-toast-{matched_toast}")
                    self._log_farm(
                        "search_toast",
                        {
                            "variant": matched_toast,
                            "resource": resource,
                            "level": level,
                            "after_tap_seconds": round(toast_elapsed, 2),
                            "screenshot": str(screenshot) if screenshot else None,
                        },
                    )
                    if matched_toast == "other_region":
                        self._farm = None
                        self._publish(
                            WorkerState.ERROR,
                            "Auto Farm dừng an toàn: toast yêu cầu tìm ở khu vực khác",
                            f"Ảnh toast: {screenshot}" if screenshot else "",
                        )
                        return
                    if self._handle_search_no_result(
                        resource,
                        level,
                        reason=f"toast:{matched_toast}",
                        delay_seconds=2.5,
                    ):
                        return
                    return
                # The popup is the positive result. Do not let the toast probe
                # consume its normal state transition.
                if state == FarmGameState.RESOURCE_POPUP:
                    self._farm_find_resource_click_at = 0.0
                elif toast_elapsed < 4.0:
                    self._farm_next_at = time.monotonic() + 0.35
                    self._publish(WorkerState.RUNNING, "Auto Farm: đang quan sát toast hoặc popup sau Tìm kiếm")
                    return
                else:
                    # The ADB execution service treats a search panel that
                    # remains usable after its observation window as a
                    # bounded no-result attempt.  The website often does not
                    # render a toast for that outcome, so do not stall on the
                    # same Search button or classify it as a technical error.
                    # Rotate resource first. A verified area change is allowed
                    # only after the whole four-resource round has no result.
                    resource, level = self._farm.current_target()
                    self._farm_find_resource_click_at = 0.0
                    if self._handle_search_no_result(
                        resource,
                        level,
                        reason="search_button_remained_visible",
                        delay_seconds=1.0,
                        after_tap_seconds=toast_elapsed,
                    ):
                        return
                    return
            # A full target can expire before the selected march is sent. The
            # game shows a confirmation dialog in that exact case. Never
            # infer this from a red button alone: the invariant message prefix
            # and Confirm button must both match on a fresh frame.
            if (
                target_resource_expiry_toast.found
                and target_resource_expiry_confirm.actionable
            ):
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_toast = fresh.evidence_for(FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_TOAST)
                fresh_confirm = fresh.evidence_for(FarmTemplateId.BROWSER_TARGET_RESOURCE_EXPIRY_CONFIRM)
                if fresh_toast.found and fresh_confirm.actionable:
                    self.session.tap_farm_template(fresh_confirm.bounds, fresh_size)  # type: ignore[arg-type]
                    self._farm_dispatch_click_at = time.monotonic()
                    self._farm_next_at = self._farm_dispatch_click_at + 0.9
                    self._log_farm("confirm_target_resource_expiry", {
                        "bounds": fresh_confirm.bounds,
                        "team": self._farm.team,
                    })
                    self._publish(
                        WorkerState.RUNNING,
                        "Auto Farm: mục tiêu sắp biến mất; đã bấm Xác nhận, đang xác minh đoàn quân xuất phát",
                    )
                    return
                self._farm_next_at = time.monotonic() + 0.4
                self._publish(WorkerState.RUNNING, "Auto Farm: dialog mục tiêu thay đổi, đang nhận diện lại")
                return
            # Once the team action was clicked, a normal World Map view is an
            # expected post-condition, not an unknown game state.  Require
            # both the persistent canvas anchor and disappearance of the team
            # panel/action before finishing this one-shot dispatch.
            if self._farm_dispatch_click_at > 0:
                elapsed_after_dispatch = time.monotonic() - self._farm_dispatch_click_at
                dispatched = (
                    # The expiry confirmation can arrive shortly *after* the
                    # team panel disappears.  Keep that short observation
                    # window open so the dialog above is handled before a
                    # normal World Map is recorded as a completed dispatch.
                    elapsed_after_dispatch >= 4.0
                    and browser_canvas.found
                    and not team_panel.found
                    and not team_action.found
                )
                if dispatched:
                    completed_team = self._farm.team
                    self._farm.decide(
                        FarmGameState.TEAM_SELECTION,
                        dispatch_verified=True,
                    )
                    self._log_farm(
                        "dispatch_verified",
                        {"team": completed_team, "elapsed_seconds": round(elapsed_after_dispatch, 2)},
                    )
                    # A completed dispatch must not leave the old workflow in
                    # WAITING forever (nor retain its dispatch timestamp).
                    # Start a fresh randomized cycle after the requested
                    # 15-second delay so the roster is scanned again.
                    self._reset_farm_cycle()
                    self._farm_next_at = time.monotonic() + self._farm.policy.retry_delay_seconds
                    self._log_farm(
                        "next_cycle_scheduled",
                        {"after_seconds": self._farm.policy.retry_delay_seconds, "previous_team": completed_team},
                    )
                    self._publish(
                        WorkerState.RUNNING,
                        f"Auto Farm: đội {completed_team} đã xuất phát; chờ 15 giây rồi quét lại đội sẵn sàng",
                    )
                    return
                if elapsed_after_dispatch <= 8.0:
                    self._farm_next_at = time.monotonic() + 0.45
                    if elapsed_after_dispatch < 4.0:
                        self._publish(
                            WorkerState.RUNNING,
                            "Auto Farm: đang chờ xác nhận mục tiêu sắp biến mất hoặc đoàn quân xuất phát",
                        )
                    else:
                        self._publish(WorkerState.RUNNING, "Auto Farm: đã bấm Thu thập, đang xác minh đoàn quân xuất phát")
                    return
                screenshot = self._save_farm_debug_capture("dispatch-unverified")
                self._log_farm(
                    "error",
                    {"reason": "dispatch_unverified", "team": self._farm.team, "screenshot": str(screenshot) if screenshot else None},
                )
                self._farm = None
                self._publish(
                    WorkerState.ERROR,
                    "Auto Farm dừng: không xác minh được đoàn quân xuất phát sau khi bấm Thu thập",
                    f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                )
                return
            # The website fades the normal HUD while World Map is loading.
            # After the first City click this is a one-way transition: never
            # click the City control again. The same control is a toggle in
            # the portal and a second click can return the player to City.
            elapsed_after_click = time.monotonic() - self._farm_world_map_click_at
            if (
                self._farm_world_map_click_at > 0
                and state == FarmGameState.CITY
                and search_button.actionable
            ):
                # Some World Map skins retain the City-looking HUD icon, but
                # expose the verified resource-search control. It is a safe
                # post-click World Map confirmation and authorises opening the
                # search panel without pressing the City toggle again.
                self._farm_world_map_click_at = 0.0
                decision = self._farm.decide(
                    FarmGameState.WORLD_MAP,
                    ready_teams=self._farm_ready_teams,
                )
                self._farm_next_at = time.monotonic() + 0.35
                self._log_farm(
                    "world_map_verified",
                    {
                        "method": "resource_search_anchor",
                        "elapsed_seconds": round(elapsed_after_click, 2),
                        "ready_teams": self._farm_ready_teams,
                    },
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: World Map đã xác minh qua nút tìm tài nguyên; {decision.message}",
                )
                return
            if self._farm_world_map_click_at > 0 and state == FarmGameState.WORLD_MAP:
                # A slow portal transition may become verifiable only after
                # the normal observation window. Accept fresh, explicit World
                # Map evidence before evaluating the timeout; otherwise a
                # successfully opened map is incorrectly stopped at 8 seconds.
                self._farm_world_map_click_at = 0.0
                decision = self._farm.decide(
                    FarmGameState.WORLD_MAP,
                    ready_teams=self._farm_ready_teams or ready_teams,
                )
                delay = 0.35 if decision.step == FarmStep.OPEN_SEARCH else self._farm.policy.retry_delay_seconds
                self._farm_next_at = time.monotonic() + delay
                self._log_farm(
                    "world_map_verified",
                    {
                        "method": "explicit_world_map_state",
                        "elapsed_seconds": round(elapsed_after_click, 2),
                        "ready_teams": self._farm_ready_teams or ready_teams,
                    },
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: World Map đã xác minh; {decision.message}",
                )
                return
            if (
                self._farm_world_map_click_at > 0
                and state != FarmGameState.WORLD_MAP
                and elapsed_after_click <= 8.0
                and not (
                    state == FarmGameState.UNKNOWN
                    and browser_canvas.found
                    and not city.found
                )
            ):
                self._farm_next_at = time.monotonic() + 0.8
                self._publish(WorkerState.RUNNING, "Auto Farm: đang chờ World Map tải xong")
                return
            if (
                self._farm_world_map_click_at > 0
                and state == FarmGameState.UNKNOWN
                and elapsed_after_click <= 8.0
                and browser_canvas.found
                and not city.found
            ):
                self._farm_world_map_click_at = 0.0
                decision = self._farm.decide(
                    FarmGameState.WORLD_MAP,
                    ready_teams=self._farm_ready_teams,
                )
                delay = 0.8 if decision.step == FarmStep.OPEN_SEARCH else self._farm.policy.retry_delay_seconds
                self._farm_next_at = time.monotonic() + delay
                self._log_farm(
                    "world_map_verified",
                    {
                        "method": "browser_canvas_anchor",
                        "elapsed_seconds": round(elapsed_after_click, 2),
                        "ready_teams": self._farm_ready_teams,
                        "roster": [
                            {"team": row.team, "state": row.state.value, "evidence": row.evidence}
                            for row in self._farm_roster
                        ],
                    },
                )
                roster_summary = self._roster_summary(self._farm_roster)
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: World Map đã xác minh; {roster_summary}; {decision.message}",
                )
                return
            if self._farm_world_map_click_at > 0 and elapsed_after_click > 8.0:
                screenshot = self._save_farm_debug_capture("world-map-unverified")
                self._log_farm(
                    "error",
                    {"reason": "world_map_unverified_after_single_click", "screenshot": str(screenshot) if screenshot else None},
                )
                self._farm = None
                self._publish(
                    WorkerState.ERROR,
                    "Auto Farm dừng an toàn: World Map chưa được xác minh sau một lần mở",
                    f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                )
                return
            if (
                state == FarmGameState.CITY
                and search_button.actionable
                and browser_canvas.found
                and self._farm.step in {FarmStep.ENTER_WORLD_MAP, FarmStep.CHECK_TEAMS}
            ):
                # On this website the World Map HUD still contains the artwork
                # used by the City template. A live canvas anchor plus the
                # resource-search button is stronger, independent evidence of
                # World Map; classify it before the generic City branch can
                # wait or try the toggle again.
                self._farm_world_map_click_at = 0.0
                decision = self._farm.decide(
                    FarmGameState.WORLD_MAP,
                    ready_teams=self._farm_ready_teams or ready_teams,
                )
                self._farm_next_at = time.monotonic() + 0.35
                self._log_farm(
                    "world_map_verified",
                    {
                        "method": "canvas_and_resource_search",
                        "ready_teams": self._farm_ready_teams or ready_teams,
                    },
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: World Map đã xác minh; {decision.message}",
                )
                return
            decision = self._farm.decide(
                state,
                ready_teams=ready_teams,
                target_verified=city.actionable,
                team_selected=expected_team_selected,
            )
            if self._farm_return_city_click_at > 0:
                elapsed_after_return = time.monotonic() - self._farm_return_city_click_at
                # The parchment/compass toggle is itself strong City evidence.
                # Accept it after a return click even if a weak coordinate-HUD
                # match momentarily misclassifies the new frame as World Map.
                city_after_return = (
                    state == FarmGameState.CITY
                    or (city.actionable and not map_to_city.actionable)
                )
                if city_after_return:
                    self._farm_return_city_click_at = 0.0
                    if state != FarmGameState.CITY:
                        decision = self._farm.decide(
                            FarmGameState.CITY,
                            ready_teams=ready_teams,
                            target_verified=True,
                        )
                    self._log_farm("city_verified_for_cycle", {"elapsed_seconds": round(elapsed_after_return, 2)})
                elif elapsed_after_return <= 8.0:
                    self._farm_next_at = time.monotonic() + 0.7
                    self._publish(WorkerState.RUNNING, "Auto Farm: đã yêu cầu về City, đang xác minh")
                    return
                else:
                    fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                    fresh_map_to_city = fresh.evidence_for(FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON)
                    if (
                        fresh.state == DetectedGameState.WORLD_MAP
                        and fresh_map_to_city.actionable
                        and self._farm_return_city_clicks < 2
                    ):
                        # Retry once with touch; the first CDP mouse click can
                        # be swallowed while the canvas is finishing an
                        # animation. The fresh template bounds prevent a blind
                        # second tap.
                        self.session.tap_farm_template(fresh_map_to_city.bounds, fresh_size)  # type: ignore[arg-type]
                        self._farm_return_city_clicks += 1
                        self._farm_return_city_click_at = time.monotonic()
                        self._farm_next_at = self._farm_return_city_click_at + 1.2
                        self._log_farm(
                            "retry_return_to_city",
                            {
                                "bounds": fresh_map_to_city.bounds,
                                "method": "touch",
                                "attempt": self._farm_return_city_clicks,
                                "control": "world_map_castle",
                            },
                        )
                        self._publish(WorkerState.RUNNING, "Auto Farm: đang thử quay về City lần 2")
                        return
                    screenshot = self._save_farm_debug_capture("city-unverified")
                    self._log_farm(
                        "error",
                        {"reason": "city_unverified_before_cycle", "screenshot": str(screenshot) if screenshot else None},
                    )
                    self._farm = None
                    self._publish(
                        WorkerState.ERROR,
                        "Auto Farm dừng an toàn: chưa xác minh được City trước cycle mới",
                        f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                    )
                    return
            if decision.step == FarmStep.RETURN_TO_CITY and state == FarmGameState.WORLD_MAP:
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_map_to_city = fresh.evidence_for(FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON)
                if fresh.state != DetectedGameState.WORLD_MAP or not fresh_map_to_city.actionable:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: chờ nút City ổn định trước cycle mới")
                    return
                # Only the freshly matched castle control is allowed to
                # return to City. Never substitute the opposite-direction
                # parchment toggle when the state is ambiguous.
                self.session.tap_farm_template(fresh_map_to_city.bounds, fresh_size)  # type: ignore[arg-type]
                method = "touch"
                self._farm_return_city_click_at = time.monotonic()
                self._farm_return_city_clicks += 1
                self._log_farm(
                    "tap_return_to_city",
                    {
                        "bounds": fresh_map_to_city.bounds,
                        "method": method,
                        "attempt": self._farm_return_city_clicks,
                        "control": "world_map_castle",
                    },
                )
                self._farm_next_at = time.monotonic() + 1.2
                self._publish(WorkerState.RUNNING, "Auto Farm: đang về City để bắt đầu cycle mới")
                return
            if (
                self._farm.step == FarmStep.OPEN_SEARCH
                and state in {FarmGameState.CITY, FarmGameState.WORLD_MAP}
                # On some portal skins the magnifier used to prove World Map
                # is also the control that opens the resource-search panel.
                # Its dedicated search template can score below threshold even
                # though the World Map anchor has already been verified.
                # Prefer the dedicated template, but allow that verified
                # anchor as a strictly scoped fallback here only.
                and (
                    search_button.actionable
                    or world.actionable
                    or world_map_coordinate_pin.actionable
                )
            ):
                if self._farm_search_clicks >= 2:
                    screenshot = self._save_farm_debug_capture("resource-search-unverified")
                    self._log_farm(
                        "error",
                        {"reason": "resource_search_unverified", "screenshot": str(screenshot) if screenshot else None},
                    )
                    self._farm = None
                    self._publish(
                        WorkerState.ERROR,
                        "Auto Farm dừng: panel tìm tài nguyên chưa được xác minh sau 2 lần thử",
                        f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                    )
                    return
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_button = fresh.evidence_for(FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON)
                fresh_world = fresh.evidence_for(FarmTemplateId.WORLD_MAP_ANCHOR)
                fresh_coordinate_pin = fresh.evidence_for(FarmTemplateId.BROWSER_WORLD_MAP_COORDINATE_PIN)
                if fresh_button.actionable:
                    click_bounds = fresh_button.bounds
                    method = "resource_search_button"
                elif fresh_world.actionable:
                    click_bounds = fresh_world.bounds
                    method = "world_map_anchor_fallback"
                elif fresh.state == DetectedGameState.WORLD_MAP and fresh_coordinate_pin.actionable:
                    # The coordinate HUD proves World Map but is not itself a
                    # button. The magnifier slot is safe only in this state.
                    click_bounds = self._world_map_search_layout_bounds(fresh_size)
                    method = "verified_world_map_layout_fallback"
                else:
                    screenshot = self._save_farm_debug_capture("resource-search-button-lost")
                    self._farm_next_at = time.monotonic() + 0.8
                    self._log_farm(
                        "resource_search_button_lost",
                        {"screenshot": str(screenshot) if screenshot else None},
                    )
                    self._publish(WorkerState.RUNNING, "Auto Farm: nút tìm tài nguyên thay đổi, đang nhận diện lại")
                    return
                self.session.tap_farm_template(click_bounds, fresh_size)  # type: ignore[arg-type]
                self._farm_search_clicks += 1
                self._farm_next_at = time.monotonic() + 1.5
                self._log_farm(
                    "tap_resource_search",
                    {"bounds": click_bounds, "method": method},
                )
                self._publish(WorkerState.RUNNING, "Auto Farm: đã mở tìm tài nguyên, đang xác minh panel")
                return
            if decision.step == FarmStep.ENTER_WORLD_MAP and city.actionable:
                if self._farm_city_clicks > 0:
                    # The one-way guard above normally owns this state. Keep
                    # this defensive branch so no later workflow change can
                    # re-introduce the destructive second toggle click.
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: đã gửi lệnh mở World Map, chỉ đang xác minh")
                    return
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_city = fresh.evidence_for(FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
                if fresh.state != DetectedGameState.CITY or not fresh_city.actionable:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: City thay đổi, bỏ click và nhận diện lại")
                    return
                self.session.tap_farm_template(fresh_city.bounds, fresh_size)  # type: ignore[arg-type]
                self._farm_city_clicks += 1
                self._farm_world_map_click_at = time.monotonic()
                self._log_farm(
                    "tap_city_to_world_map",
                    {"bounds": fresh_city.bounds, "method": "touch"},
                )
                self._farm_next_at = time.monotonic() + 1.2
                self._publish(
                    WorkerState.RUNNING,
                    "Auto Farm: đã mở World Map, đang xác minh",
                )
                return
            if (
                decision.step == FarmStep.OPEN_TEAM_SELECTION
                and state == FarmGameState.RESOURCE_POPUP
                and gather_button.actionable
            ):
                if self._farm_gather_clicks >= 2:
                    screenshot = self._save_farm_debug_capture("gather-unverified")
                    self._log_farm(
                        "error",
                        {"reason": "gather_unverified", "screenshot": str(screenshot) if screenshot else None},
                    )
                    self._farm = None
                    self._publish(
                        WorkerState.ERROR,
                        "Auto Farm dừng: không xác minh được panel chọn đội sau 2 lần Thu thập",
                        f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                    )
                    return
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_gather = fresh.evidence_for(FarmTemplateId.BROWSER_GATHER_BUTTON_ENABLED)
                if not fresh_gather.actionable:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: nút Thu thập thay đổi, đang nhận diện lại")
                    return
                self.session.tap_farm_template(fresh_gather.bounds, fresh_size)  # type: ignore[arg-type]
                self._farm_gather_clicks += 1
                self._farm_next_at = time.monotonic() + 1.5
                self._log_farm(
                    "tap_gather",
                    {"bounds": fresh_gather.bounds, "resource": decision.resource, "level": decision.level},
                )
                self._publish(WorkerState.RUNNING, "Auto Farm: đã bấm Thu thập, đang xác minh chọn đội")
                return
            if decision.step == FarmStep.SELECT_TEAM and state == FarmGameState.TEAM_SELECTION:
                target_row = self._team_row_for_selection(
                    decision.team,
                    team_badges,
                    image_size,
                )
                if target_row is None:
                    screenshot = self._save_farm_debug_capture("expected-team-not-visible")
                    self._farm_next_at = time.monotonic() + self._farm.policy.retry_delay_seconds
                    self._log_farm(
                        "expected_team_not_visible",
                        {"team": decision.team, "screenshot": str(screenshot) if screenshot else None},
                    )
                    self._publish(
                        WorkerState.RUNNING,
                        f"Auto Farm: không xác định được hàng đội {decision.team} trên panel chọn đội; đang chờ",
                    )
                    return
                if self._farm_team_selection_clicks >= 2:
                    screenshot = self._save_farm_debug_capture("team-selection-unverified")
                    self._log_farm(
                        "error",
                        {
                            "reason": "team_selection_unverified",
                            "team": decision.team,
                            "screenshot": str(screenshot) if screenshot else None,
                        },
                    )
                    self._farm = None
                    self._publish(
                        WorkerState.ERROR,
                        f"Auto Farm dừng: không xác minh được đội {decision.team} sau 2 lần chọn",
                        f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                    )
                    return
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_panel = fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL)
                fresh_action = fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON)
                fresh_badges = {
                    2: fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_2_BADGE),
                    3: fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_3_BADGE),
                    4: fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_4_BADGE),
                }
                row_bounds = self._team_row_for_selection(
                    decision.team,
                    fresh_badges,
                    fresh_size,
                )
                if not (
                    fresh_panel.actionable
                    and fresh_action.actionable
                    and row_bounds is not None
                ):
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: panel chọn đội thay đổi, đang nhận diện lại")
                    return
                self.session.tap_farm_template(row_bounds, fresh_size)
                self._farm_expected_team_row = row_bounds
                self._farm_team_selection_clicks += 1
                self._farm_next_at = time.monotonic() + 1.2
                self._log_farm(
                    "tap_expected_team",
                    {
                        "team": decision.team,
                        "badge_bounds": getattr(fresh_badges.get(decision.team), "bounds", None),
                        "row_bounds": row_bounds,
                        "method": "inferred_first_row" if decision.team == 1 else "numbered_badge",
                    },
                )
                self._publish(WorkerState.RUNNING, f"Auto Farm: đã chọn đội {decision.team}, đang xác minh viền chọn")
                return
            if decision.step == FarmStep.DISPATCH and state == FarmGameState.TEAM_SELECTION:
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_panel = fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_SELECTION_PANEL)
                fresh_action = fresh.evidence_for(FarmTemplateId.BROWSER_TEAM_ACTION_BUTTON)
                if not fresh_panel.actionable or not fresh_action.actionable:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: panel điều quân thay đổi, đang nhận diện lại")
                    return
                self.session.tap_farm_template(fresh_action.bounds, fresh_size)  # type: ignore[arg-type]
                self._farm_dispatch_click_at = time.monotonic()
                self._log_farm(
                    "tap_dispatch",
                    {"team": decision.team, "bounds": fresh_action.bounds},
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: đã bấm Thu thập với đội {decision.team}, đang xác minh đoàn quân xuất phát",
                )
                self._farm_next_at = time.monotonic() + 1.5
                return
            if decision.step == FarmStep.FIND_RESOURCE and state == FarmGameState.RESOURCE_SEARCH:
                target_resource = decision.resource or ""
                target_resource_button = resource_buttons.get(target_resource)
                target_resource_active = resource_active_buttons.get(target_resource)
                # Do not skip a resource tap based only on the active-state
                # artwork. In production the four active templates can all
                # match the same panel background, which made the worker claim
                # Iron/Wood/Stone was selected while Food remained active.
                # A plan target must therefore be explicitly tapped first.
                if self._farm_resource_selected_at <= 0 and target_resource_button and target_resource_button.actionable:
                    # ``detected`` above is a fresh renderer capture for this
                    # tick. Taking another CDP WebGL screenshot immediately
                    # before this click can return a transient frame and lose
                    # the already confirmed Wood match. Use the same fresh
                    # evidence, then verify the next UI state after input.
                    self.session.tap_farm_template(target_resource_button.bounds, image_size)  # type: ignore[arg-type]
                    self._farm_resource_selected_at = time.monotonic()
                    self._farm_resource_selected_by_layout = False
                    self._farm_resource_template_misses = 0
                    self._farm_next_at = self._farm_resource_selected_at + 1.2
                    self._log_farm(
                        "tap_resource",
                        {"resource": decision.resource, "bounds": target_resource_button.bounds, "level": decision.level},
                    )
                    self._publish(WorkerState.RUNNING, f"Auto Farm: đã chọn {decision.resource} cấp {decision.level}, đang xác minh")
                    return
                if self._farm_resource_panel_verified and self._farm_resource_selected_at <= 0:
                    # The resource panel is already verified, but the target
                    # artwork may still be animating into the compact canvas.
                    # Keep polling quickly; do not return to the 30-second
                    # farm retry cadence or re-open the tab.
                    self._farm_resource_template_misses += 1
                    # Each resource button occupies a stable slot in the
                    # already independently verified web search panel. Some
                    # city skins recolour the icon art enough that a visual
                    # template never reaches its threshold (Food scored 0.37
                    # on account-3 while the panel and Search button scored
                    # above 0.95). After three bounded observations, use the
                    # panel-relative slot rather than waiting indefinitely.
                    if self._farm_resource_template_misses >= 3 and find_resource.actionable:
                        layout_bounds = self._resource_button_layout_bounds(target_resource, image_size)
                        self.session.tap_farm_template(layout_bounds, image_size)
                        self._farm_resource_selected_at = time.monotonic()
                        self._farm_resource_selected_by_layout = True
                        self._farm_next_at = self._farm_resource_selected_at + 1.0
                        self._log_farm(
                            "tap_resource",
                            {
                                "resource": target_resource,
                                "level": decision.level,
                                "bounds": layout_bounds,
                                "method": "verified_panel_layout_fallback",
                            },
                        )
                        self._publish(
                            WorkerState.RUNNING,
                            f"Auto Farm: đã chọn {target_resource} theo bố cục panel đã xác minh, đang kiểm tra lại",
                        )
                        return
                    self._farm_next_at = time.monotonic() + 0.5
                    self._publish(
                        WorkerState.RUNNING,
                        f"Auto Farm: đang nhận diện nút {decision.resource} cấp {decision.level} "
                        f"({self._farm_resource_template_misses})",
                    )
                    return
                if self._farm_resource_selected_at > 0:
                    elapsed = time.monotonic() - self._farm_resource_selected_at
                    # The Search button is deliberately gated by a separate
                    # selected-state template.  Previously the worker only
                    # waited for a timeout after tapping an inactive icon, so
                    # it could keep waiting forever and never submit Search.
                    active_verified = bool(target_resource_active and target_resource_active.found)
                    # The click above is allowed only after the resource panel
                    # and enabled Search control were both verified. On skins
                    # where icon art differs, preserve that verified layout
                    # post-condition rather than falsely failing a successful
                    # selection solely because the active icon template is
                    # from another skin.
                    if self._farm_resource_selected_by_layout and find_resource.actionable:
                        active_verified = True
                    if not active_verified:
                        if elapsed >= 5.0:
                            screenshot = self._save_farm_debug_capture("resource-active-unverified")
                            self._log_farm(
                                "error",
                                {
                                    "reason": "resource_active_unverified",
                                    "resource": target_resource,
                                    "level": decision.level,
                                    "active": self._evidence_payload(target_resource_active),
                                    "screenshot": str(screenshot) if screenshot else None,
                                },
                            )
                            self._farm = None
                            self._publish(
                                WorkerState.ERROR,
                                f"Auto Farm dừng: chưa xác minh {target_resource} đang được chọn",
                                f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                            )
                            return
                        self._farm_next_at = time.monotonic() + 0.45
                        self._publish(
                            WorkerState.RUNNING,
                            f"Auto Farm: đang xác minh {target_resource} đã active trước khi bấm Tìm kiếm",
                        )
                        return
                    # The game's search option is opt-in.  Never hit Search
                    # until the unchecked glyph has either been changed by a
                    # verified tap or is absent on the freshly matched panel.
                    if not self._farm_target_checkbox_verified:
                        if self._farm_target_checkbox_click_at > 0:
                            checkbox_elapsed = time.monotonic() - self._farm_target_checkbox_click_at
                            if not target_checkbox_unchecked.found and find_resource.actionable:
                                self._farm_target_checkbox_verified = True
                                self._log_farm(
                                    "search_target_checkbox_verified",
                                    {"elapsed_seconds": round(checkbox_elapsed, 2)},
                                )
                                self._publish(
                                    WorkerState.RUNNING,
                                    "Auto Farm: đã bật lọc mục tiêu tìm kiếm, đang tiếp tục tìm tài nguyên",
                                )
                                self._farm_next_at = time.monotonic() + 0.35
                                return
                            if checkbox_elapsed >= 4.0:
                                screenshot = self._save_farm_debug_capture("search-target-checkbox-unverified")
                                self._log_farm(
                                    "error",
                                    {
                                        "reason": "search_target_checkbox_unverified",
                                        "screenshot": str(screenshot) if screenshot else None,
                                    },
                                )
                                self._farm = None
                                self._publish(
                                    WorkerState.ERROR,
                                    "Auto Farm dừng: chưa xác minh được checkbox lọc mục tiêu trước Tìm kiếm",
                                    f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                                )
                                return
                            self._farm_next_at = time.monotonic() + 0.5
                            self._publish(WorkerState.RUNNING, "Auto Farm: đang xác minh checkbox lọc mục tiêu")
                            return
                        fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                        fresh_checkbox = fresh.evidence_for(
                            FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_UNCHECKED
                        )
                        checkbox_bounds = fresh_checkbox.bounds if fresh_checkbox.actionable else None
                        method = "template"
                        # A failed unchecked-template match is not proof that
                        # the option is checked. With a live panel and enabled
                        # Search button, tap its fixed panel-relative slot once
                        # and verify the post-click frame before Search.
                        if checkbox_bounds is None and find_resource.actionable:
                            checkbox_bounds = self._search_target_checkbox_layout_bounds(fresh_size)
                            method = "verified_panel_layout_fallback"
                        if checkbox_bounds is not None:
                            self.session.tap_farm_template(checkbox_bounds, fresh_size)
                            self._farm_target_checkbox_click_at = time.monotonic()
                            self._farm_target_checkbox_clicks += 1
                            self._log_farm(
                                "tap_search_target_checkbox",
                                {"bounds": checkbox_bounds, "method": method},
                            )
                            self._publish(
                                WorkerState.RUNNING,
                                "Auto Farm: đã bật checkbox lọc mục tiêu, đang xác minh",
                            )
                            self._farm_next_at = self._farm_target_checkbox_click_at + 0.8
                            return
                        self._farm_next_at = time.monotonic() + 0.5
                        self._publish(WorkerState.RUNNING, "Auto Farm: đang chờ checkbox lọc mục tiêu")
                        return
                    if elapsed >= 0.8 and find_resource.actionable:
                        if self._farm_find_resource_clicks >= 2:
                            screenshot = self._save_farm_debug_capture("find-resource-unverified")
                            self._log_farm("error", {"reason": "find_resource_unverified", "screenshot": str(screenshot) if screenshot else None})
                            self._farm = None
                            self._publish(WorkerState.ERROR, "Auto Farm dừng: không xác minh được tài nguyên sau khi tìm 2 lần", f"Ảnh trước khi dừng: {screenshot}" if screenshot else "")
                            return
                        fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                        fresh_find_resource = fresh.evidence_for(FarmTemplateId.BROWSER_SEARCH_BUTTON_ENABLED)
                        if fresh_find_resource.actionable:
                            self.session.tap_farm_template(fresh_find_resource.bounds, fresh_size)  # type: ignore[arg-type]
                            self._farm_find_resource_clicks += 1
                            self._farm_find_resource_click_at = time.monotonic()
                            self._farm_next_at = self._farm_find_resource_click_at + 0.6
                            self._log_farm("tap_find_resource", {"bounds": fresh_find_resource.bounds, "resource": decision.resource, "level": decision.level})
                            self._publish(WorkerState.RUNNING, f"Auto Farm: đang tìm {decision.resource} cấp {decision.level}, đang xác minh mục tiêu")
                            return
                    if elapsed >= 5.0:
                        screenshot = self._save_farm_debug_capture("resource-unverified")
                        self._log_farm("error", {"reason": "resource_unverified", "resource": decision.resource, "screenshot": str(screenshot) if screenshot else None})
                        self._farm = None
                        self._publish(
                            WorkerState.ERROR,
                            f"Auto Farm dừng: lựa chọn {decision.resource} chưa được xác minh",
                            f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                        )
                        return
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, f"Auto Farm: đang chờ lựa chọn {decision.resource} cập nhật")
                    return
                if self._farm_resource_tab_clicked_at <= 0 and resource_tab.actionable:
                    fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                    fresh_tab = fresh.evidence_for(FarmTemplateId.BROWSER_RESOURCE_TAB_BUTTON)
                    if fresh_tab.actionable:
                        self.session.tap_farm_template(fresh_tab.bounds, fresh_size)  # type: ignore[arg-type]
                        self._farm_resource_tab_clicked_at = time.monotonic()
                        self._farm_next_at = self._farm_resource_tab_clicked_at + 1.2
                        self._log_farm("tap_resource_tab", {"bounds": fresh_tab.bounds})
                        self._publish(WorkerState.RUNNING, "Auto Farm: đã chọn tab Tài nguyên, đang xác minh")
                        return
                if self._farm_resource_tab_clicked_at > 0:
                    elapsed = time.monotonic() - self._farm_resource_tab_clicked_at
                    if not resource_tab.found:
                        self._farm_resource_panel_verified = True
                        self._farm_resource_tab_clicked_at = 0.0
                        self._farm_resource_template_misses = 0
                        self._farm_next_at = time.monotonic() + 0.35
                        self._log_farm("resource_tab_verified", {"next": "select_resource"})
                        self._publish(
                            WorkerState.RUNNING,
                            f"Auto Farm: tab Tài nguyên đã xác minh; đang chọn {decision.resource} cấp {decision.level}",
                        )
                        return
                    if elapsed >= 5.0:
                        screenshot = self._save_farm_debug_capture("resource-tab-unverified")
                        self._log_farm("error", {"reason": "resource_tab_unverified", "screenshot": str(screenshot) if screenshot else None})
                        self._farm = None
                        self._publish(
                            WorkerState.ERROR,
                            "Auto Farm dừng: tab Tài nguyên chưa được xác minh",
                            f"Ảnh trước khi dừng: {screenshot}" if screenshot else "",
                        )
                        return
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: đang chờ tab Tài nguyên cập nhật")
                    return
                screenshot = self._save_farm_debug_capture("resource-plan-pending")
                self._farm_next_at = time.monotonic() + self._farm.policy.retry_delay_seconds
                self._log_farm(
                    "resource_plan_pending",
                    {
                        "resource": decision.resource,
                        "level": decision.level,
                        "screenshot": str(screenshot) if screenshot else None,
                    },
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: panel đã xác minh; chờ template chọn {decision.resource} cấp {decision.level}",
                    f"Ảnh để port bước tiếp theo: {screenshot}" if screenshot else "",
                )
                return
            if decision.step == FarmStep.WAITING and state == FarmGameState.WORLD_MAP:
                screenshot = self._save_farm_debug_capture("blocked-no-ready-team")
                self._log_farm(
                    "blocked_no_ready_team",
                    {"screenshot": str(screenshot) if screenshot else None},
                )
                self._farm_next_at = time.monotonic() + self._farm.policy.retry_delay_seconds
                self._publish(
                    WorkerState.RUNNING,
                    "Auto Farm: World Map đã xác minh; không có đội sẵn sàng",
                    f"Ảnh kiểm tra: {screenshot}" if screenshot else "",
                )
                return
            self._farm_next_at = time.monotonic() + 1.0
            if state == FarmGameState.UNKNOWN:
                # Keep the worker safe, but expose the two decisive scores so
                # a canvas/theme mismatch can be diagnosed without guessing.
                screenshot = self._save_farm_debug_capture("unknown")
                world_score = world.confidence
                city_score = city.confidence
                self._log_farm(
                    "unknown_state",
                    {
                        "city_confidence": city_score,
                        "world_map_confidence": world_score,
                        "screenshot": str(screenshot) if screenshot else None,
                    },
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: {decision.message} (City={city_score:.2f}; World Map={world_score:.2f})",
                    f"Log: {self.farm_log.path}" + (f" | Ảnh debug: {screenshot}" if screenshot else ""),
                )
                return
            self._publish(WorkerState.RUNNING, f"Auto Farm: {decision.message}")
        except RuntimeError as error:
            if "Cửa sổ game đang bị thu nhỏ hoặc bị cửa sổ khác che" in str(error):
                self._farm_capture_blocked_count += 1
                screenshot = self._save_farm_debug_capture("capture-blocked")
                self._log_farm(
                    "capture_blocked",
                    {
                        "attempt": self._farm_capture_blocked_count,
                        "screenshot": str(screenshot) if screenshot else None,
                    },
                )
                if self._farm_capture_blocked_count < 10:
                    self._farm_next_at = time.monotonic() + 1.0
                    self._publish(
                        WorkerState.RUNNING,
                        "Auto Farm: cửa sổ game tạm thời bị che, đang chờ capture lại",
                    )
                    return
            screenshot = self._save_farm_debug_capture("error")
            self._log_farm(
                "error",
                {"type": type(error).__name__, "message": str(error), "screenshot": str(screenshot) if screenshot else None},
            )
            self._farm = None
            self._publish(WorkerState.ERROR, f"Auto Farm đã dừng an toàn: {error}")
        except Exception as error:
            screenshot = self._save_farm_debug_capture("error")
            self._log_farm(
                "error",
                {"type": type(error).__name__, "message": str(error), "screenshot": str(screenshot) if screenshot else None},
            )
            self._farm = None
            self._publish(WorkerState.ERROR, f"Auto Farm đã dừng an toàn: {error}")

    def _reset_farm_cycle(self) -> None:
        """Create a clean farm cycle without stopping the active worker."""
        self._farm = FarmWorkflow()
        self._farm_city_clicks = 0
        self._farm_return_city_click_at = 0.0
        self._farm_return_city_clicks = 0
        self._farm_world_map_click_at = 0.0
        self._farm_ready_teams = ()
        self._farm_roster = ()
        self._farm_search_clicks = 0
        self._farm_resource_tab_clicked_at = 0.0
        self._farm_resource_panel_verified = False
        self._farm_resource_template_misses = 0
        self._farm_resource_selected_at = 0.0
        self._farm_resource_selected_by_layout = False
        self._farm_target_checkbox_click_at = 0.0
        self._farm_target_checkbox_verified = False
        self._farm_target_checkbox_clicks = 0
        self._farm_find_resource_clicks = 0
        self._farm_find_resource_click_at = 0.0
        self._farm_gather_clicks = 0
        self._farm_capture_blocked_count = 0
        self._farm_team_selection_clicks = 0
        self._farm_expected_team_row = None
        self._farm_dispatch_click_at = 0.0
        self._farm_area_selector = ResourceAreaPointSelector()
        self._farm_area_epoch = 0
        self._farm_run_id = f"{self.profile.id}-{time.monotonic_ns()}"

    def _handle_search_no_result(
        self,
        resource: str,
        level: int,
        *,
        reason: str,
        delay_seconds: float,
        after_tap_seconds: float | None = None,
    ) -> bool:
        """Advance one bounded no-result outcome without retrying a target.

        The four randomized resource types are exhausted first at the current
        level. Only then can the browser use the verified World Map coordinate
        workflow. This prevents an early area jump after a single missing
        resource and preserves the ADB point-selector's three-point limit.
        """
        if self._farm is None:
            return False
        if self._farm.advance_search_plan():
            next_resource, next_level = self._farm.current_target()
            self._farm_resource_selected_at = 0.0
            self._farm_resource_selected_by_layout = False
            self._farm_resource_template_misses = 0
            self._farm_find_resource_clicks = 0
            self._farm_find_resource_click_at = 0.0
            self._log_farm(
                "search_no_result",
                {
                    "reason": reason,
                    "resource": resource,
                    "level": level,
                    "next_resource": next_resource,
                    "next_level": next_level,
                    "round_complete": False,
                    "after_tap_seconds": round(after_tap_seconds, 2) if after_tap_seconds is not None else None,
                },
            )
            self._farm_next_at = time.monotonic() + delay_seconds
            self._publish(
                WorkerState.RUNNING,
                f"Auto Farm: không có {resource} cấp {level}; đổi sang {next_resource} cấp {next_level}",
            )
            return True

        # All four resource types have been tried at this level. The workflow
        # now owns a new point selection, which is shuffled and non-repeating
        # per run/profile/resource/level/area epoch by ResourceAreaPointSelector.
        area_resource, area_level = self._farm.current_target()
        relocation = self._try_resource_area_relocation(area_resource, area_level)
        if relocation == "moved":
            self._farm_resource_selected_at = 0.0
            self._farm_resource_selected_by_layout = False
            self._farm_resource_template_misses = 0
            self._farm_target_checkbox_click_at = 0.0
            self._farm_target_checkbox_verified = False
            self._farm_target_checkbox_clicks = 0
            self._farm_find_resource_clicks = 0
            self._farm_find_resource_click_at = 0.0
            self._log_farm(
                "search_round_area_relocated",
                {"reason": reason, "level": area_level, "resource": area_resource},
            )
            self._farm_next_at = time.monotonic() + 1.0
            return True
        if relocation == "unavailable":
            # Do not skip to another level if the map/input UI cannot be
            # verified. The coordinate method can retry later without blind
            # interaction and without losing the selected search plan.
            self._log_farm(
                "search_round_area_waiting",
                {"reason": reason, "level": area_level, "resource": area_resource},
            )
            self._farm_next_at = time.monotonic() + self._farm.policy.retry_delay_seconds
            self._publish(
                WorkerState.RUNNING,
                f"Auto Farm: đã thử 4 tài nguyên cấp {area_level}; chờ xác minh World Map để đổi khu vực",
            )
            return True

        # The approved three-point pool for this level is exhausted. Only now
        # move to the next configured level; if all pools ended, finish this
        # cycle and let the normal 15-second scheduler start a fresh one.
        if self._farm.advance_level_plan():
            next_resource, next_level = self._farm.current_target()
            self._farm_resource_selected_at = 0.0
            self._farm_resource_selected_by_layout = False
            self._farm_resource_template_misses = 0
            self._farm_find_resource_clicks = 0
            self._farm_find_resource_click_at = 0.0
            self._log_farm(
                "search_area_pool_exhausted",
                {"reason": reason, "level": area_level, "next_resource": next_resource, "next_level": next_level},
            )
            self._farm_next_at = time.monotonic() + delay_seconds
            self._publish(
                WorkerState.RUNNING,
                f"Auto Farm: hết 3 điểm khu vực cho cấp {area_level}; chuyển sang {next_resource} cấp {next_level}",
            )
            return True
        self._farm_next_at = time.monotonic() + self._farm.policy.retry_delay_seconds
        self._publish(WorkerState.COMPLETED, "Auto Farm: đã thử hết tài nguyên, cấp mỏ và điểm khu vực; chờ cycle tiếp theo")
        return True

    @staticmethod
    def _resource_button_layout_bounds(
        resource: str, image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Return one resource slot inside the verified web search panel.

        This is deliberately expressed as canvas proportions, never desktop
        pixels. It is used only after the search panel *and* enabled Search
        button have been matched; it is a bounded fallback for skins whose
        resource artwork differs from our visual template pack.
        """
        centers = {
            "food": (0.286, 0.735),
            "wood": (0.406, 0.735),
            "stone": (0.526, 0.735),
            "iron": (0.646, 0.735),
        }
        center_x, center_y = centers.get(resource, centers["food"])
        width, height = image_size
        button_width = max(36, round(width * 0.095))
        button_height = max(36, round(height * 0.18))
        left = max(0, min(width - button_width, round(width * center_x - button_width / 2)))
        top = max(0, min(height - button_height, round(height * center_y - button_height / 2)))
        return (left, top, button_width, button_height)

    @staticmethod
    def _world_map_search_layout_bounds(
        image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Return the magnifier slot on a verified World Map canvas.

        Browser skins change the magnifier artwork, but retain its HUD slot.
        This is deliberately used only after the World Map coordinate HUD has
        been freshly matched, never as a blind desktop-coordinate click.
        """
        width, height = image_size
        button_width = max(38, round(width * 0.058))
        button_height = max(36, round(height * 0.104))
        left = max(0, min(width - button_width, round(width * 0.325 - button_width / 2)))
        top = max(0, min(height - button_height, round(height * 0.807 - button_height / 2)))
        return (left, top, button_width, button_height)

    @staticmethod
    def _search_target_checkbox_layout_bounds(
        image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Return the checkbox slot within a matcher-verified search panel."""
        width, height = image_size
        box_width = max(20, round(width * 0.04))
        box_height = max(20, round(height * 0.065))
        left = max(0, min(width - box_width, round(width * 0.717 - box_width / 2)))
        top = max(0, min(height - box_height, round(height * 0.718 - box_height / 2)))
        return (left, top, box_width, box_height)

    def _try_resource_area_relocation(self, resource: str, level: int) -> str:
        """Move to one verified map coordinate through the website's City UI.

        All canvas interactions are guarded by a fresh template match. The
        browser adapter returns from World Map to City, opens City's map icon,
        then requires readable DOM coordinate inputs before it edits either
        field. This keeps the original pair available for a rollback if the
        destination cannot be verified.
        """
        if self.session is None or self._farm is None:
            return "unavailable"
        selection = self._farm_area_selector.next(
            run_id=self._farm_run_id,
            profile_id=self.profile.id,
            resource=resource,
            level=level,
            area_epoch=self._farm_area_epoch,
        )
        if selection.exhausted:
            self._log_farm("resource_area_exhausted", {
                "resource": resource, "level": level, "max_attempts": selection.max_attempts,
                "city_levels": selection.city_levels,
            })
            return "exhausted"
        point = selection.point
        assert point is not None
        detected, _surface, size = self.session.detect_farm_state()
        # The web game does not open Continent Map from World Map directly.
        # First return to City using the verified blue back control, then use
        # City's dedicated map icon. This is intentionally different from the
        # old ADB minimap shortcut and matches the portal UI contract.
        return_to_city = detected.evidence_for(FarmTemplateId.BROWSER_WORLD_MAP_BACK_BUTTON)
        if not return_to_city.actionable:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "world_map_return_button_unavailable", "point": point,
                "attempt": selection.attempt, "city_levels": selection.city_levels,
            })
            return "unavailable"
        self.session.tap_farm_template(return_to_city.bounds, size)  # type: ignore[arg-type]
        self._log_farm(
            "tap_world_map_return_to_city",
            {"bounds": return_to_city.bounds, "point": point},
        )
        time.sleep(0.7)
        city, _surface, city_size = self.session.detect_farm_state()
        city_map_button = city.evidence_for(FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON)
        if city.state != DetectedGameState.CITY or not city_map_button.actionable:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "city_or_city_map_button_unverified", "point": point,
                "attempt": selection.attempt,
            })
            return "unavailable"
        self.session.tap_farm_template(city_map_button.bounds, city_size)  # type: ignore[arg-type]
        self._log_farm(
            "tap_city_continent_map",
            {"bounds": city_map_button.bounds, "point": point},
        )
        time.sleep(0.6)
        continent, _surface, continent_size = self.session.detect_farm_state()
        if continent.state != DetectedGameState.CONTINENT_MAP:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "continent_map_unverified", "point": point, "attempt": selection.attempt,
            })
            return "unavailable"
        pin = continent.evidence_for(FarmTemplateId.CONTINENT_MAP_PIN_BUTTON)
        if not pin.actionable:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "continent_map_pin_unavailable", "point": point, "attempt": selection.attempt,
            })
            return "unavailable"
        x_field, y_field = self._coordinate_fields_from_pin(pin.bounds, continent_size)  # type: ignore[arg-type]
        original_x = self.session.read_focused_numeric_farm_input(x_field, continent_size)
        original_y = self.session.read_focused_numeric_farm_input(y_field, continent_size)
        if original_x is None or original_y is None:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "original_coordinates_unreadable", "point": point, "attempt": selection.attempt,
            })
            return "unavailable"
        if not self.session.read_focused_numeric_farm_input(x_field, continent_size) == original_x:
            return "unavailable"
        if not self.session.replace_focused_farm_input(point[0]):
            return "unavailable"
        if not self.session.read_focused_numeric_farm_input(y_field, continent_size) == original_y or not self.session.replace_focused_farm_input(point[1]):
            self.session.read_focused_numeric_farm_input(x_field, continent_size)
            self.session.replace_focused_farm_input(original_x)
            return "unavailable"
        refreshed, _surface, refreshed_size = self.session.detect_farm_state()
        refreshed_pin = refreshed.evidence_for(FarmTemplateId.CONTINENT_MAP_PIN_BUTTON)
        if not refreshed_pin.actionable:
            return "unavailable"
        self.session.tap_farm_template(refreshed_pin.bounds, refreshed_size)  # type: ignore[arg-type]
        time.sleep(0.45)
        target_frame, _surface, target_size = self.session.detect_farm_state()
        target_pin = target_frame.evidence_for(FarmTemplateId.CONTINENT_MAP_SEARCH_TARGET_PIN)
        if not target_pin.actionable:
            rollback_pin = target_frame.evidence_for(FarmTemplateId.CONTINENT_MAP_PIN_BUTTON)
            if rollback_pin.actionable:
                rollback_x, rollback_y = self._coordinate_fields_from_pin(rollback_pin.bounds, target_size)  # type: ignore[arg-type]
                self.session.read_focused_numeric_farm_input(rollback_x, target_size)
                self.session.replace_focused_farm_input(original_x)
                self.session.read_focused_numeric_farm_input(rollback_y, target_size)
                self.session.replace_focused_farm_input(original_y)
                self.session.tap_farm_template(rollback_pin.bounds, target_size)  # type: ignore[arg-type]
            self._log_farm("resource_area_navigation_rolled_back", {
                "reason": "destination_pin_unverified", "point": point,
                "original": (original_x, original_y), "attempt": selection.attempt,
            })
            return "unavailable"
        self.session.tap_farm_template(target_pin.bounds, target_size)  # type: ignore[arg-type]
        time.sleep(0.7)
        final, _surface, _final_size = self.session.detect_farm_state()
        world_verified = final.state == DetectedGameState.WORLD_MAP or (
            final.evidence_for(FarmTemplateId.BROWSER_CANVAS_READY_ANCHOR).found
            and final.evidence_for(FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON).found
        )
        if not world_verified:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "world_map_unverified_after_target_pin", "point": point,
                "attempt": selection.attempt,
            })
            return "unavailable"
        self._farm.step = FarmStep.OPEN_SEARCH
        self._farm_area_epoch += 1
        self._log_farm("resource_area_relocated", {
            "resource": resource, "level": level, "point": point,
            "attempt": selection.attempt, "max_attempts": selection.max_attempts,
            "city_levels": selection.city_levels, "original": (original_x, original_y),
        })
        self._publish(WorkerState.RUNNING, f"Auto Farm: đã chuyển tới {point[0]},{point[1]}; mở lại tìm {resource} cấp {level}")
        return "moved"

    @staticmethod
    def _coordinate_fields_from_pin(
        pin_bounds: tuple[int, int, int, int], image_size: tuple[int, int]
    ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """Port the ADB's pin-relative X/Y input geometry to this canvas."""
        left, top, width, height = pin_bounds
        image_width, image_height = image_size
        center_x = left + width // 2
        center_y = top + height // 2
        x_center = center_x + round(-134 * image_width / 1280)
        y_center = center_y + round(-54 * image_height / 720)
        return (x_center, center_y, 2, 2), (center_x, y_center, 2, 2)

    def _log_farm(self, event: str, payload: dict[str, object]) -> None:
        self.farm_log.write(event, {"profile_id": self.profile.id, **payload})

    @staticmethod
    def _evidence_payload(evidence: object) -> dict[str, object]:
        return {
            "found": bool(getattr(evidence, "found", False)),
            "confidence": round(float(getattr(evidence, "confidence", 0.0)), 4),
            "bounds": getattr(evidence, "bounds", None),
        }

    @staticmethod
    def _roster_summary(roster: tuple[TeamRosterRow, ...]) -> str:
        if not roster:
            return "chưa đọc được roster"
        ready = ",".join(str(row.team) for row in roster if row.state.value == "ready") or "không có"
        busy = ",".join(str(row.team) for row in roster if row.state.value == "busy") or "không có"
        return f"đã quét {len(roster)} đội (sẵn sàng: {ready}; bận: {busy})"

    @staticmethod
    def _is_expected_team_selected(
        expected_badge: object,
        selected_border: object,
        expected_row_bounds: tuple[int, int, int, int] | None,
    ) -> bool:
        border_bounds = getattr(selected_border, "bounds", None)
        if not getattr(selected_border, "actionable", False) or border_bounds is None:
            return False
        # Prefer the row recorded from the fresh pre-click match.  The visual
        # number on a team badge may be confused with a similarly shaped hero
        # icon after selection, while the selected-row border is stable.
        if expected_row_bounds is not None:
            _left, row_top, _width, row_height = expected_row_bounds
            border_center = int(border_bounds[1]) + max(1, int(border_bounds[3])) // 2
            return row_top <= border_center <= row_top + row_height
        badge_bounds = getattr(expected_badge, "bounds", None)
        if not getattr(expected_badge, "actionable", False) or badge_bounds is None:
            return False
        return abs(int(border_bounds[1]) - int(badge_bounds[1])) <= max(
            12,
            int(badge_bounds[3]) * 2,
        )

    @staticmethod
    def _team_row_from_badge(
        badge_bounds: tuple[int, int, int, int], image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Resolve a safe row target from the freshly matched numbered badge.

        This mirrors the ADB selector: the badge identifies the expected team,
        then the tap lands in its enclosing row rather than on a fixed screen
        coordinate or dynamic hero artwork.
        """
        left, top, width, height = badge_bounds
        image_width, image_height = image_size
        row_left = max(0, left - max(4, width // 3))
        row_top = max(0, top - max(6, height // 3))
        row_width = min(image_width - row_left, max(width * 6, 48))
        row_height = min(image_height - row_top, max(height * 4, 48))
        if row_width <= 0 or row_height <= 0:
            raise ValueError("Không xác định được vùng chọn đội an toàn")
        return row_left, row_top, row_width, row_height

    @classmethod
    def _team_row_for_selection(
        cls,
        team: int | None,
        badges: dict[int, object],
        image_size: tuple[int, int],
    ) -> tuple[int, int, int, int] | None:
        """Resolve all four team rows without inventing a Team 1 badge.

        The game labels rows 2–4 but the first row has no numbered badge. For
        team 1, infer its badge slot from the freshly verified rows 2 and 3,
        then reuse the same bounded row geometry as every other team.
        """
        if team in {2, 3, 4}:
            badge = badges.get(team)
            bounds = getattr(badge, "bounds", None)
            if not getattr(badge, "actionable", False) or bounds is None:
                return None
            return cls._team_row_from_badge(bounds, image_size)
        if team != 1:
            return None
        team_2 = badges.get(2)
        team_2_bounds = getattr(team_2, "bounds", None)
        if not getattr(team_2, "actionable", False) or team_2_bounds is None:
            return None
        team_3 = badges.get(3)
        team_3_bounds = getattr(team_3, "bounds", None)
        row_stride = 0
        if getattr(team_3, "actionable", False) and team_3_bounds is not None:
            row_stride = int(team_3_bounds[1]) - int(team_2_bounds[1])
        if row_stride <= 0:
            row_stride = max(1, round(image_size[1] * 0.21))
        virtual_team_1_badge = (
            int(team_2_bounds[0]),
            max(0, int(team_2_bounds[1]) - row_stride),
            int(team_2_bounds[2]),
            int(team_2_bounds[3]),
        )
        return cls._team_row_from_badge(virtual_team_1_badge, image_size)

    def _save_farm_debug_capture(self, reason: str) -> Path | None:
        if self.session is None:
            return None
        png = self.session.last_farm_capture_png()
        if not png:
            return None
        folder = self.config.data_dir / "screenshots" / self.profile.id / "farm-debug"
        path = folder / f"{reason}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        return write_retained_png(path, upscale_png_for_diagnostics(png), keep=10)

    def _close_session(self) -> None:
        self._farm = None
        if self.session is None:
            return
        try:
            self.session.close()
        finally:
            self.session = None

    def _handle_external_close(self) -> None:
        self._farm = None
        if self.session is None:
            return
        try:
            self.session.close()
        finally:
            self.session = None
        self._publish(WorkerState.STOPPED, "Cửa sổ Chrome đã đóng")


class MultiProfileRunner:
    def __init__(
        self,
        config: AppConfig,
        on_update: UpdateCallback,
        on_coordinate: CoordinateCallback | None = None,
    ) -> None:
        self.config = config
        self.on_update = on_update
        self.on_coordinate = on_coordinate or (lambda _profile_id, _event: None)
        self.event_log = JsonLineLog(config.data_dir / "logs" / "events.jsonl")
        self.workers: dict[str, ProfileWorker] = {}
        self.sync_enabled = False
        self.sync_master_id: str | None = None
        self.drag_items_visible = False
        self.scrollbars_visible = False
        self.windows_topmost = False
        self._resource_cpu_samples: dict[str, tuple[float, float]] = {}
        self._sync_lock = threading.Lock()
        self.sync_profiles()

    def sync_profiles(self) -> None:
        configured = {profile.id: profile for profile in self.config.profiles}
        for profile_id in list(self.workers):
            if profile_id not in configured:
                worker = self.workers.pop(profile_id)
                worker.shutdown()
        for profile in self.config.profiles:
            if profile.id not in self.workers:
                self.workers[profile.id] = ProfileWorker(
                    self.config,
                    profile,
                    self.event_log,
                    self.on_update,
                    self._on_input,
                    self.on_coordinate,
                    drag_item_visible=self.drag_items_visible,
                    scrollbars_visible=self.scrollbars_visible,
                    topmost=self.windows_topmost,
                )

    def submit(self, profile_id: str, kind: CommandKind, **payload: object) -> None:
        self.workers[profile_id].submit(WorkerCommand(kind, dict(payload)))

    def open_all(self) -> None:
        for profile in self.config.profiles:
            if profile.enabled:
                self.submit(profile.id, CommandKind.OPEN)

    def read_all(self) -> None:
        for profile in self.config.profiles:
            if profile.enabled:
                self.submit(profile.id, CommandKind.READ)

    def resize_all(self, width: int, height: int) -> None:
        for profile in self.config.profiles:
            if profile.enabled:
                self.submit(profile.id, CommandKind.RESIZE, width=width, height=height)

    def enable_sync(self, master_id: str) -> None:
        if master_id not in self.workers:
            raise KeyError(f"Không tìm thấy profile master: {master_id}")
        with self._sync_lock:
            self.sync_enabled = True
            self.sync_master_id = master_id
        for profile_id in self.workers:
            self.submit(
                profile_id,
                CommandKind.SET_SYNC_SOURCE,
                enabled=profile_id == master_id,
            )

    def disable_sync(self) -> None:
        with self._sync_lock:
            self.sync_enabled = False
            self.sync_master_id = None
        for profile_id in self.workers:
            self.submit(profile_id, CommandKind.SET_SYNC_SOURCE, enabled=False)

    def set_inspector(self, profile_id: str, enabled: bool) -> None:
        self.submit(profile_id, CommandKind.SET_INSPECTOR, enabled=enabled)

    def set_drag_item_visible(self, profile_id: str, visible: bool) -> None:
        self.submit(profile_id, CommandKind.SET_DRAG_ITEM, visible=visible)

    def set_all_drag_items_visible(self, visible: bool) -> int:
        self.drag_items_visible = bool(visible)
        count = 0
        for profile in self.config.profiles:
            worker = self.workers.get(profile.id)
            if worker is None:
                continue
            worker.submit(
                WorkerCommand(CommandKind.SET_DRAG_ITEM, {"visible": self.drag_items_visible})
            )
            if worker.session is not None:
                count += 1
        return count

    def set_all_scrollbars_visible(self, visible: bool) -> int:
        self.scrollbars_visible = bool(visible)
        count = 0
        for profile in self.config.profiles:
            worker = self.workers.get(profile.id)
            if worker is None:
                continue
            worker.submit(
                WorkerCommand(CommandKind.SET_SCROLLBARS, {"visible": self.scrollbars_visible})
            )
            if worker.session is not None:
                count += 1
        return count

    def set_all_topmost(self, enabled: bool) -> int:
        self.windows_topmost = bool(enabled)
        count = 0
        for profile in self.config.profiles:
            worker = self.workers.get(profile.id)
            if worker is None:
                continue
            worker.submit(
                WorkerCommand(CommandKind.SET_TOPMOST, {"enabled": self.windows_topmost})
            )
            if worker.session is not None:
                count += 1
        return count

    def arrange_windows(
        self,
        columns_per_row: int | None = None,
        *,
        profile_ids: set[str] | None = None,
    ) -> int:
        """Tile the requested profiles left-to-right, then top-to-bottom."""
        if columns_per_row is not None and not 1 <= int(columns_per_row) <= 6:
            raise ValueError("Số cửa sổ mỗi hàng phải từ 1 đến 6")
        opened: list[tuple[str, int, WindowRect, WindowRect]] = []
        for profile in self.config.profiles:
            if profile_ids is not None and profile.id not in profile_ids:
                continue
            worker = self.workers.get(profile.id)
            session = worker.session if worker else None
            if session is None:
                continue
            hwnd = session.window_handle
            if hwnd is None:
                continue
            try:
                outer = get_window_rect(hwnd)
                visible = get_visible_window_rect(hwnd)
            except Exception:
                continue
            opened.append((profile.id, hwnd, outer, visible))
        if not opened:
            return 0
        columns = int(columns_per_row or len(opened))
        rows = max(1, (len(opened) + columns - 1) // columns)
        work_area = get_work_area()
        # Keep every game surface at 16:9 while fitting the requested grid.
        # The resulting windows still tile edge-to-edge; unused space (when
        # the screen aspect ratio cannot fit the final row exactly) stays at
        # the outside of the grid rather than between profiles.
        max_width_by_columns = max(1, work_area.width // columns)
        max_width_by_rows = max(1, (work_area.height // rows) * 16 // 9)
        visible_width = min(max_width_by_columns, max_width_by_rows)
        visible_height = max(1, visible_width * 9 // 16)
        positions = calculate_tiled_positions(
            work_area,
            visible_width,
            visible_height,
            len(opened),
            columns_per_row=columns,
        )
        moved = 0
        for (profile_id, _hwnd, outer, visible), (x, y) in zip(
            opened, positions, strict=True
        ):
            worker = self.workers.get(profile_id)
            session = worker.session if worker else None
            if session is None:
                continue
            try:
                # Resize the full Chrome frame to its grid cell as well as
                # moving it. This makes a column selection visibly apply to
                # profiles already running, rather than only to future opens.
                frame_width = visible_width + (outer.width - visible.width)
                frame_height = visible_height + (outer.height - visible.height)
                move_window_outer(
                    _hwnd,
                    x - (visible.left - outer.left),
                    y - (visible.top - outer.top),
                    frame_width,
                    frame_height,
                    topmost=self.windows_topmost,
                )
                moved += 1
            except Exception:
                continue
        return moved

    def has_open_session(self, profile_id: str) -> bool:
        worker = self.workers.get(profile_id)
        return bool(worker and worker.session is not None)

    def resource_overview(self) -> ResourceOverview:
        now = time.monotonic()
        cpu_count = max(1, os.cpu_count() or 1)
        process_parents = snapshot_process_parents()
        rows: list[ProfileResourceSnapshot] = []
        for profile in self.config.profiles:
            worker = self.workers.get(profile.id)
            session = worker.session if worker else None
            hwnd = session.window_handle if session is not None else None
            if hwnd is None:
                self._resource_cpu_samples.pop(profile.id, None)
                rows.append(ProfileResourceSnapshot(profile.id, False))
                continue
            usage = get_window_process_tree_usage(hwnd, process_parents)
            previous = self._resource_cpu_samples.get(profile.id)
            cpu_percent = 0.0
            if previous is not None:
                previous_at, previous_cpu = previous
                elapsed = now - previous_at
                if elapsed > 0:
                    cpu_percent = max(
                        0.0,
                        min(
                            100.0,
                            (usage.cpu_seconds - previous_cpu)
                            / (elapsed * cpu_count)
                            * 100.0,
                        ),
                    )
            self._resource_cpu_samples[profile.id] = (now, usage.cpu_seconds)
            rows.append(
                ProfileResourceSnapshot(
                    profile.id,
                    True,
                    usage.process_count,
                    usage.working_set_bytes,
                    cpu_percent,
                )
            )
        return ResourceOverview(
            total_profiles=len(self.config.profiles),
            opened_profiles=sum(row.opened for row in rows),
            process_count=sum(row.process_count for row in rows),
            ram_bytes=sum(row.ram_bytes for row in rows),
            cpu_percent=sum(row.cpu_percent for row in rows),
            profiles=tuple(rows),
        )

    def trim_all_profile_memory(self) -> int:
        trimmed = 0
        process_parents = snapshot_process_parents()
        for worker in self.workers.values():
            session = worker.session
            hwnd = session.window_handle if session is not None else None
            if hwnd is not None:
                trimmed += trim_window_process_tree(hwnd, process_parents)
        return trimmed

    def _on_input(self, source_profile_id: str, event: dict[str, object]) -> None:
        with self._sync_lock:
            enabled = self.sync_enabled
            master_id = self.sync_master_id
        if not enabled or source_profile_id != master_id:
            return
        for profile_id, worker in self.workers.items():
            if profile_id == source_profile_id or worker.session is None:
                continue
            worker.submit(WorkerCommand(CommandKind.SYNC_INPUT, {"event": event}))

    def stop_all(self) -> None:
        for worker in self.workers.values():
            worker.stop()

    def shutdown(self) -> None:
        self.disable_sync()
        for worker in self.workers.values():
            worker.shutdown()
        for worker in self.workers.values():
            worker.join()
