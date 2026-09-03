from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ik_chrome_auto.actions import ActionCancelled, AutomationFunctions
from ik_chrome_auto.browser import ChromeProfileSession
from ik_chrome_auto.build_info import release_diagnostic_screenshot_directory
from ik_chrome_auto.event_log import JsonLineLog, migrate_legacy_profile_log, profile_log_path
from ik_chrome_auto.farm_vision import DetectedGameState, FarmTemplateId, TeamRosterRow
from ik_chrome_auto.farm_workflow import FarmGameState, FarmStep, FarmWorkflow
from ik_chrome_auto.image_utils import decode_png
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
from ik_chrome_auto.mail_monitor import (
    COMBAT_MAIL_OTHER,
    MAIL_BASELINE,
    NO_NEW_COMBAT_MAIL,
    SCAN_CANCELLED,
    SCAN_ERROR,
    TERRITORY_ATTACKED,
    BrowserMailMonitor,
)
from ik_chrome_auto.windows import (
    calculate_tiled_positions,
    get_monitor_work_areas,
    get_visible_window_rect,
    get_window_rect,
    get_window_process_tree_usage,
    is_window_minimized,
    move_window_outer,
    set_window_minimized,
    snapshot_process_parents,
    trim_window_process_tree,
    WindowRect,
)

UpdateCallback = Callable[[WorkerSnapshot], None]
InputCallback = Callable[[str, dict[str, object]], None]
CoordinateCallback = Callable[[str, dict[str, object]], None]

# Fixed monitor controls are expressed as X/Y ratios of the game canvas, not
# desktop or Chrome-window pixels.  The canonical capture is 16:9, but width
# and height are intentionally scaled independently: a compact 5-window row
# and a 1280x720 screenshot therefore receive the same relative tap.
MONITOR_REFERENCE_ASPECT_RATIO = 16 / 9

# Farm UI fallbacks use the same canvas-relative coordinate contract as the
# monitor.  These are screen controls inside the game canvas, not World Map
# coordinates.  The 16:9 aspect ratio is the authored default; the X and Y
# values are still applied independently to the live canvas dimensions so a
# compact profile viewport keeps the same relative target.
FARM_REFERENCE_ASPECT_RATIO = 16 / 9
# A 366×168 canvas has too little original information for team and button
# recognition.  The worker therefore temporarily renders a single active
# profile at this true 16:9 size; it never upscales a compact screenshot.
AUTOMATION_RENDERER_SIZE = (1280, 720)
# The browser bootstrap now removes the portal/iframe document's default 8 px
# body margin. The native renderer must therefore be exactly 1280×720 too;
# retaining the old +16 compensation makes the game itself render at
# 1296×736, drops all template scores, and causes the lease/restore cycle to
# look like repeated zooming.
AUTOMATION_RENDERER_CANVAS_GUTTER = (0, 0)
AUTOMATION_RENDERER_WINDOW_SIZE = tuple(
    canvas + gutter
    for canvas, gutter in zip(
        AUTOMATION_RENDERER_SIZE,
        AUTOMATION_RENDERER_CANVAS_GUTTER,
        strict=True,
    )
)
FARM_MINIMUM_CANVAS_SIZE = AUTOMATION_RENDERER_SIZE
# One high-detail WebGL renderer at a time prevents five compact profiles from
# becoming five simultaneous 720p GPU surfaces.  A Farm lease survives the
# short internal workflow steps and is released for long waits; monitoring
# holds the same lease only for its bounded mailbox flow.
AUTOMATION_RENDERER_WAIT_SECONDS = 30.0
# A Farm state that takes a second or more to settle (map loading, an unknown
# frame, a search result) must yield the one true 1280×720 renderer.  Keeping
# the old four-second lease made the first profile monopolise it indefinitely
# while another profile was waiting for its initial scan.
FARM_RENDERER_IDLE_RELEASE_SECONDS = 0.9
FARM_NO_READY_TEAM_RESCAN_SECONDS = 120.0
FARM_MAP_TRANSITION_RENDERER_HOLD_SECONDS = 2.0
# A click is not complete until its fresh 1280×720 postcondition has been
# observed.  This must exceed the two-second Search verification delay, so the
# grid cannot shrink between the click and the capture that proves it worked.
FARM_CONTROL_POSTCONDITION_HOLD_SECONDS = 2.2
# World Map can show a blank, fading canvas for well over eight seconds while
# the portal loads.  The transition has already been clicked exactly once, so
# wait for an explicit map control instead of aborting mid-load.
FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS = 35.0
# The game can leave World Map visible while it streams the resource popup.
# Do not rotate to another resource during that transition: a late popup is a
# valid positive result and must still reach its Gather button.
FARM_SEARCH_RESULT_SETTLE_SECONDS = 10.0
# Controls the capped retry backoff, not whether Farm may stop. A Farm run is
# intentionally persistent and ends only through the user's Stop Farm command.
FARM_MAX_RECOVERY_ATTEMPTS = 3
FARM_RECOVERY_BASE_DELAY_SECONDS = 3.0
FARM_RESOURCE_BUTTON_CENTERS: dict[str, tuple[float, float]] = {
    "food": (0.286, 0.735),
    "wood": (0.406, 0.735),
    "stone": (0.526, 0.735),
    "iron": (0.646, 0.735),
}
FARM_RESOURCE_BUTTON_SIZE = (0.095, 0.18)
# The City/World Map toggle keeps the same bottom-left slot in both directions.
# Its centre was measured at (53, 666) on the real 1280×720 game canvas.  The
# matcher verifies the current screen, but the tap deliberately uses this
# canvas-relative slot so a false visual bound can never redirect it to Mail.
FARM_MAP_TOGGLE_CENTER = (53 / 1280, 666 / 720)
FARM_MAP_TOGGLE_SIZE = (80 / 1280, 90 / 720)
# Supplied 1280x720 World Map frames place the magnifier at
# (425, 552, 57, 57). Keep the fallback on that exact canvas-relative slot;
# the previous X ratio resolved around 416 and missed the button centre.
FARM_WORLD_MAP_SEARCH_CENTER = (453.5 / 1280, 580.5 / 720)
FARM_WORLD_MAP_SEARCH_SIZE = (57 / 1280, 57 / 720)
# The search panel initially opens on the monster category.  Its bottom-left
# category button switches the same panel to the four resource types.  These
# ratios are measured from the supplied 1280x720 renderer frame; they are a
# fallback only after the resource-search panel itself has been verified.
FARM_RESOURCE_TAB_CENTER = (210 / 1280, 670.5 / 720)
FARM_RESOURCE_TAB_SIZE = (92 / 1280, 47 / 720)
# Measured from the supplied resource panel: the round checkbox is above the
# Search button's left edge, at x≈1022 on the real 1280×720 renderer.  The old
# 0.717 ratio landed on explanatory text instead of the toggle.
FARM_SEARCH_TARGET_CHECKBOX_CENTER = (1022 / 1280, 517 / 720)
FARM_SEARCH_TARGET_CHECKBOX_SIZE = (0.04, 0.065)
FARM_TEAM_ROW_WIDTH_RATIO = 0.172
FARM_TEAM_ROW_HEIGHT_RATIO = 0.19
FARM_TEAM_ROW_FIRST_TOP_RATIO = 0.002
FARM_TEAM_ROW_STRIDE_RATIO = 0.205
# The coordinate fields sit a fixed *relative canvas distance* from the
# freshly matched Continent Map pin.  Keeping these as ratios avoids treating
# the old 1280x720 capture as a desktop pixel coordinate system.
FARM_COORDINATE_X_FIELD_OFFSET_X_RATIO = -134 / 1280
FARM_COORDINATE_Y_FIELD_OFFSET_Y_RATIO = -54 / 720

# Measured from the 1260×674 video supplied on 2026-08-28.  They are declared
# as direct X/Y canvas percentages (rather than retained pixel references), so
# each profile maps them against its own live CDP game-surface capture.
#
# The intended 16:9 canvas is the default. Width and height are mapped
# independently so a compact or slightly cropped browser canvas remains safe.
MAIL_BUTTON_POINT = (0.1151, 0.8086)
COMBAT_TAB_POINT = (0.0635, 0.3650)
READ_ALL_MAIL_POINT = (0.2008, 0.9110)
CLOSE_MAIL_POINT = (0.9437, 0.1142)
# The top card only: never search down the Combat list for a historical match.
FIRST_MAIL_ROW_POINT = (0.2516, 0.1736)
MAIL_CONTROL_SETTLE_SECONDS = 0.9


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
        automation_renderer_lock: threading.Lock | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.event_log = event_log
        self.on_update = on_update
        self.on_input = on_input
        self.on_coordinate = on_coordinate
        logs_dir = config.data_dir / "logs"
        self.coordinate_log = JsonLineLog(logs_dir / f"coordinates-{profile.id}.jsonl")
        # Farm diagnostics stay separate from general dashboard events so a
        # failed template can be investigated without sifting through UI logs.
        farm_log_path = profile_log_path(logs_dir, "farm", profile.name, profile.id)
        migrate_legacy_profile_log(logs_dir / f"farm-{profile.id}.jsonl", farm_log_path)
        self.farm_log = JsonLineLog(
            farm_log_path,
            max_bytes=2_000_000,
            backups=2,
        )
        self.stop_event = threading.Event()
        self.commands: queue.Queue[WorkerCommand] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.session: ChromeProfileSession | None = None
        self._thread_lock = threading.Lock()
        self._sync_source_enabled = False
        self._sync_rearm_at = 0.0
        self._inspector_enabled = False
        self._drag_item_visible = drag_item_visible
        self._scrollbars_visible = scrollbars_visible
        self._topmost = topmost
        self._automation_renderer_lock = automation_renderer_lock or threading.Lock()
        self._automation_renderer_locked = False
        self._automation_renderer_layout: Any | None = None
        self._automation_renderer_hold_until = 0.0
        self._farm_renderer_waiting = False
        self._farm: FarmWorkflow | None = None
        self._farm_next_at = 0.0
        self._farm_city_clicks = 0
        self._farm_return_city_click_at = 0.0
        self._farm_return_city_clicks = 0
        self._farm_world_map_click_at = 0.0
        self._farm_ready_teams: tuple[int, ...] = ()
        self._farm_roster: tuple[TeamRosterRow, ...] = ()
        self._farm_post_dispatch_roster_scan_pending = False
        self._farm_search_clicks = 0
        self._farm_resource_tab_clicked_at = 0.0
        self._farm_resource_panel_verified = False
        self._farm_resource_template_misses = 0
        self._farm_resource_selected_at = 0.0
        self._farm_resource_selected_by_layout = False
        self._farm_detected_resource_level: int | None = None
        self._farm_target_checkbox_click_at = 0.0
        self._farm_target_checkbox_verified = False
        self._farm_target_checkbox_seen_unchecked = False
        self._farm_target_checkbox_clicks = 0
        self._farm_find_resource_clicks = 0
        self._farm_find_resource_click_at = 0.0
        self._farm_gather_clicks = 0
        self._farm_capture_blocked_count = 0
        self._farm_canvas_resize_attempts = 0
        self._farm_team_selection_clicks = 0
        # The numbered badge is only used to derive the row to tap.  It can
        # resemble another badge after the row's artwork changes, so retain
        # the freshly resolved row as the authoritative post-tap target.
        self._farm_expected_team_row: tuple[int, int, int, int] | None = None
        self._farm_dispatch_click_at = 0.0
        self._farm_area_selector = ResourceAreaPointSelector()
        self._farm_area_epoch = 0
        self._farm_run_id = ""
        self._farm_area_relocation_pending: tuple[str, int] | None = None
        self._farm_area_pending_selection: Any | None = None
        self._farm_recovery_attempts = 0
        self._mail_monitor: BrowserMailMonitor | None = None
        # Stopping monitoring can also cover a MONITOR_MAIL command already
        # waiting in this worker's queue, before that command opens mail.
        self._mail_monitor_cancelled = threading.Event()

    def submit(self, command: WorkerCommand) -> None:
        self._ensure_thread()
        self.commands.put(command)

    def stop(self) -> None:
        self.stop_event.set()
        self.submit(WorkerCommand(CommandKind.STOP))

    def shutdown(self) -> None:
        self.stop_event.set()
        self.submit(WorkerCommand(CommandKind.SHUTDOWN))

    def enable_mail_monitor(self) -> None:
        self._mail_monitor_cancelled.clear()

    def cancel_mail_monitor(self) -> None:
        self._mail_monitor_cancelled.set()

    def _mail_monitor_is_cancelled(self) -> bool:
        cancelled = getattr(self, "_mail_monitor_cancelled", None)
        return bool(cancelled is not None and cancelled.is_set())

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

    def _publish_monitor(
        self, events: tuple[str, ...], checked: tuple[str, ...]
    ) -> None:
        """Return scan data without overwriting the visible profile status."""
        state = WorkerState.RUNNING if self._farm is not None else WorkerState.READY
        self.on_update(
            WorkerSnapshot(
                profile_id=self.profile.id,
                state=state,
                message="Giám sát đã quét",
                farm_roster=tuple((row.team, row.state.value) for row in self._farm_roster),
                monitor_events=events,
                monitor_checked=checked,
            )
        )

    def _monitor_pause(self, seconds: float) -> None:
        """Wait briefly without starving Playwright's browser connection."""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.session is not None:
                self.session.pump(5)
            time.sleep(min(0.08, max(0.0, deadline - time.monotonic())))

    def _acquire_automation_renderer(self, *, wait_seconds: float = 0.0) -> bool:
        """Lease a renderer that yields a true 1280×720 CDP game canvas."""
        if getattr(self, "_automation_renderer_locked", False):
            return True
        if self.session is None:
            return False
        renderer_lock = getattr(self, "_automation_renderer_lock", None)
        if renderer_lock is None:
            renderer_lock = threading.Lock()
            self._automation_renderer_lock = renderer_lock
        acquired = renderer_lock.acquire(
            timeout=max(0.0, float(wait_seconds))
        )
        if not acquired:
            return False
        self._automation_renderer_locked = True
        try:
            begin = getattr(self.session, "begin_automation_renderer", None)
            self._automation_renderer_layout = (
                begin(*AUTOMATION_RENDERER_WINDOW_SIZE) if callable(begin) else None
            )
        except Exception:
            self._automation_renderer_locked = False
            renderer_lock.release()
            raise
        return True

    def _release_automation_renderer(self, *, restore: bool = True) -> None:
        """Return the leased profile to its exact compact grid cell."""
        if not getattr(self, "_automation_renderer_locked", False):
            return
        layout = self._automation_renderer_layout
        self._automation_renderer_layout = None
        self._automation_renderer_locked = False
        try:
            restore_renderer = getattr(self.session, "restore_automation_renderer", None)
            if restore and callable(restore_renderer):
                restore_renderer(layout)
        finally:
            self._automation_renderer_lock.release()

    def _release_farm_renderer_when_idle(self) -> None:
        """Restore the grid only after the active 720p interaction is complete.

        Once a Farm input has been sent, every following capture and
        post-condition in that cycle must use the same 1280x720 renderer.
        Releasing merely because the next verification is 1-2 seconds away
        creates a compact-grid frame between click and verification.
        """
        if not getattr(self, "_automation_renderer_locked", False):
            return
        # Do not shrink from 1280x720 back to a five-column tile while the
        # game is consuming and verifying a Map transition click.
        if time.monotonic() < getattr(self, "_automation_renderer_hold_until", 0.0):
            return
        # The post-click hold above is the only period that must retain the
        # renderer. Afterwards always yield, including before a short
        # 0.35–0.8s poll: otherwise one profile can reacquire every short tick
        # and starve another profile already waiting at a resource popup. The
        # next Farm tick reacquires 1280×720 before it captures or clicks.
        self._release_automation_renderer()

    def _capture_mail_canvas(self) -> tuple[bytes, tuple[int, int]]:
        if self.session is None:
            raise RuntimeError("Profile chưa mở")
        png, _surface = self.session.capture_game_surface_png()
        image = decode_png(png)
        return png, (image.width, image.height)

    def _tap_monitor_point(
        self, normalized_x: float, normalized_y: float, image_size: tuple[int, int]
    ) -> None:
        if self.session is None:
            raise RuntimeError("Profile chưa mở")
        width, height = image_size
        x = max(1, min(width - 2, round(width * normalized_x)))
        y = max(1, min(height - 2, round(height * normalized_y)))
        # Browser template bounds are (left, top, width, height). A symmetric
        # 2x2 box makes its computed center exactly the requested X/Y point.
        #
        # Mail controls are HTML/canvas portal controls rather than gameplay
        # targets. On some Chromium/GPU combinations they ignore a synthetic
        # touch but accept a CDP mouse click. Keep the same canvas-relative
        # point and prefer mouse for this monitoring-only path; the fallback
        # retains compatibility with lightweight test and older sessions.
        bounds = (x - 1, y - 1, 2, 2)
        click_mouse = getattr(self.session, "click_farm_template_mouse", None)
        if callable(click_mouse):
            click_mouse(bounds, image_size)
        else:
            self.session.tap_farm_template(bounds, image_size)

    def _tap_monitor_viewport_point(
        self, point: tuple[float, float], image_size: tuple[int, int]
    ) -> None:
        """Tap a point normalized to the live game canvas.

        ``image_size`` comes from the fresh CDP canvas capture of this exact
        profile. It is consequently correct for any viewport size, DPI and
        number of Chrome windows per row, without relying on desktop geometry.
        """
        normalized_x, normalized_y = point
        if not (0.0 < normalized_x < 1.0 and 0.0 < normalized_y < 1.0):
            raise ValueError("Tọa độ giám sát phải là tỉ lệ X/Y trong khoảng 0..1")
        self._tap_monitor_point(normalized_x, normalized_y, image_size)

    def _check_combat_mail(self, *, initial_scan: bool) -> str:
        """Run the two-pass mailbox workflow for one profile.

        Pass 1 opens Mail and uses ``Đọc & Nhận Tất Cả`` across all mail
        categories to establish a clean baseline. Pass 2+ opens Combat and
        reads only the top item when that category carries an unread badge.
        Every transition is verified from a fresh renderer capture.
        """
        if self._mail_monitor_is_cancelled():
            return SCAN_CANCELLED
        if self.session is None:
            return SCAN_ERROR
        if not self._acquire_automation_renderer(
            wait_seconds=AUTOMATION_RENDERER_WAIT_SECONDS
        ):
            raise RuntimeError("Hết thời gian chờ renderer 1280×720 cho Giám sát")
        if self._mail_monitor is None:
            self._mail_monitor = BrowserMailMonitor()
        monitor = self._mail_monitor
        mail_open = False
        latest_png: bytes | None = None
        latest_size: tuple[int, int] | None = None
        try:
            latest_png, latest_size = self._capture_mail_canvas()
            close = monitor.find_close_button(latest_png)
            if close is None:
                self._tap_monitor_viewport_point(MAIL_BUTTON_POINT, latest_size)
                self._monitor_pause(MAIL_CONTROL_SETTLE_SECONDS)
                if self._mail_monitor_is_cancelled():
                    return SCAN_CANCELLED
                latest_png, latest_size = self._capture_mail_canvas()
                close = monitor.find_close_button(latest_png)
                if close is None:
                    raise RuntimeError("Hộp thư chưa mở sau khi bấm nút thư")
            mail_open = True

            if initial_scan:
                # Pass 1 intentionally stays on the initially opened mailbox
                # category. The game's Read All action applies to all
                # notifications, so entering Combat first would leave other
                # categories outside the requested baseline flow.
                self._tap_monitor_viewport_point(READ_ALL_MAIL_POINT, latest_size)
                self._monitor_pause(MAIL_CONTROL_SETTLE_SECONDS)
                if self._mail_monitor_is_cancelled():
                    return SCAN_CANCELLED
                latest_png, latest_size = self._capture_mail_canvas()
                if monitor.find_close_button(latest_png) is None:
                    raise RuntimeError("Hộp thư bị đóng sau khi bấm Đọc & Nhận Tất Cả")
                self.event_log.write(
                    "mail_monitor_baseline",
                    {"profile_id": self.profile.id},
                )
                return MAIL_BASELINE

            # Pass 2+: Combat is the second category on the left. Use its
            # fixed canvas-relative X/Y rather than another visual search.
            self._tap_monitor_viewport_point(COMBAT_TAB_POINT, latest_size)
            self._monitor_pause(MAIL_CONTROL_SETTLE_SECONDS)
            if self._mail_monitor_is_cancelled():
                return SCAN_CANCELLED
            latest_png, latest_size = self._capture_mail_canvas()
            if monitor.find_close_button(latest_png) is None:
                raise RuntimeError("Hộp thư bị đóng trước khi đọc tab Chiến đấu")
            if not monitor.has_new_combat_mail(latest_png):
                return NO_NEW_COMBAT_MAIL

            # Read exactly the first row so the game's unread state becomes
            # authoritative; no historical row below it is inspected.
            self._tap_monitor_viewport_point(FIRST_MAIL_ROW_POINT, latest_size)
            self._monitor_pause(MAIL_CONTROL_SETTLE_SECONDS)
            if self._mail_monitor_is_cancelled():
                return SCAN_CANCELLED
            latest_png, latest_size = self._capture_mail_canvas()
            if monitor.find_close_button(latest_png) is None:
                raise RuntimeError("Không xác minh được thư đầu tiên sau khi mở")
            if monitor.is_territory_attacked(latest_png):
                return TERRITORY_ATTACKED
            return COMBAT_MAIL_OTHER
        finally:
            try:
                if mail_open:
                    try:
                        latest_png, latest_size = self._capture_mail_canvas()
                        close = monitor.find_close_button(latest_png)
                        if close is not None:
                            self._tap_monitor_viewport_point(CLOSE_MAIL_POINT, latest_size)
                            self._monitor_pause(0.35)
                    except Exception as close_error:
                        self.event_log.write(
                            "mail_monitor_close_error",
                            {
                                "profile_id": self.profile.id,
                                "message": f"{type(close_error).__name__}: {close_error}",
                            },
                        )
            finally:
                self._release_automation_renderer()

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
                    # Tool shutdown deliberately leaves profile Chrome
                    # windows running so an update/restart can reconnect.
                    self._close_session(close_browser=False)
                    self._publish(WorkerState.STOPPED, "Đã đóng worker")
                    return
                if command.kind == CommandKind.STOP:
                    # STOP is submitted by the explicit “Đóng tabs” button;
                    # this must close the retained detached Chrome context.
                    self._close_session(close_browser=True)
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
                    self._farm_detected_resource_level = None
                    self._farm_target_checkbox_click_at = 0.0
                    self._farm_target_checkbox_verified = False
                    self._farm_target_checkbox_seen_unchecked = False
                    self._farm_target_checkbox_clicks = 0
                    self._farm_find_resource_clicks = 0
                    self._farm_find_resource_click_at = 0.0
                    self._farm_gather_clicks = 0
                    self._farm_capture_blocked_count = 0
                    self._farm_canvas_resize_attempts = 0
                    self._farm_renderer_waiting = False
                    self._farm_team_selection_clicks = 0
                    self._farm_expected_team_row = None
                    self._farm_dispatch_click_at = 0.0
                    self._farm_area_selector = ResourceAreaPointSelector()
                    self._farm_area_epoch = 0
                    self._farm_run_id = f"{self.profile.id}-{time.monotonic_ns()}"
                    self._farm_area_relocation_pending = None
                    self._farm_area_pending_selection = None
                    self._farm_recovery_attempts = 0
                    self._log_farm("started", {"resource_order": self._farm.resource_order})
                    self._publish(
                        WorkerState.RUNNING,
                        f"Auto Farm: đang preflight game canvas | thứ tự tài nguyên: {', '.join(self._farm.resource_order)}",
                    )
                    continue
                if command.kind == CommandKind.STOP_FARM:
                    self._farm = None
                    self._farm_renderer_waiting = False
                    self._release_automation_renderer()
                    self._log_farm("stopped", {"reason": "user"})
                    self._publish(WorkerState.READY if self.session is not None else WorkerState.STOPPED, "Đã dừng Auto Farm")
                    continue
                if command.kind == CommandKind.MONITOR_MAIL:
                    if self.session is None:
                        self._publish_monitor((SCAN_ERROR,), ())
                        continue
                    try:
                        event = self._check_combat_mail(
                            initial_scan=bool(command.payload.get("initial_scan", False))
                        )
                        self._publish_monitor((event,), ("combat_mail",))
                    except Exception as error:
                        self.event_log.write(
                            "mail_monitor_error",
                            {
                                "profile_id": self.profile.id,
                                "message": f"{type(error).__name__}: {error}",
                            },
                        )
                        self._publish_monitor((SCAN_ERROR,), ("combat_mail",))
                    continue
                if command.kind == CommandKind.SET_SYNC_SOURCE:
                    self._sync_source_enabled = bool(command.payload.get("enabled", False))
                    if self.session is not None:
                        armed_frames = self.session.set_sync_source(self._sync_source_enabled)
                        self._sync_rearm_at = time.monotonic() + 2.0
                        self.event_log.write(
                            "sync_source_armed",
                            {
                                "profile_id": self.profile.id,
                                "enabled": self._sync_source_enabled,
                                "armed_frame_count": int(armed_frames or 0),
                            },
                        )
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
                        sync_event = dict(command.payload["event"])
                        try:
                            self._apply_synced_input_with_retry(sync_event)
                        except Exception as error:
                            # One transient frame navigation must not mark the
                            # whole profile Error or disable subsequent input.
                            self.event_log.write(
                                "sync_input_error",
                                {
                                    "profile_id": self.profile.id,
                                    "type": str(sync_event.get("type", "")),
                                    "message": f"{type(error).__name__}: {error}",
                                },
                            )
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

    def _apply_synced_input_with_retry(self, event: dict[str, Any]) -> None:
        """Retry once after repairing a follower's stale frame/CDP runtime."""
        if self.session is None:
            return
        try:
            self.session.apply_synced_input(event)
            return
        except Exception as first_error:
            repair = getattr(self.session, "repair_synced_input_runtime", None)
            if not callable(repair):
                raise
            repair()
            try:
                self.session.apply_synced_input(event)
            except Exception:
                raise first_error
            self.event_log.write(
                "sync_input_recovered",
                {
                    "profile_id": self.profile.id,
                    "type": str(event.get("type", "")),
                },
            )

    def _poll_browser_events(self) -> None:
        if self.session is None:
            return
        if self._sync_source_enabled:
            now = time.monotonic()
            if now >= self._sync_rearm_at:
                try:
                    armed_frames = self.session.sync_capture_frame_count()
                    if armed_frames <= 0:
                        armed_frames = self.session.set_sync_source(True)
                        self.event_log.write(
                            "sync_source_rearmed",
                            {
                                "profile_id": self.profile.id,
                                "armed_frame_count": int(armed_frames or 0),
                            },
                        )
                finally:
                    self._sync_rearm_at = now + 2.0
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
        if not self._acquire_automation_renderer():
            self._farm_next_at = time.monotonic() + 0.5
            if not self._farm_renderer_waiting:
                self._farm_renderer_waiting = True
                self._publish(
                    WorkerState.RUNNING,
                    "Auto Farm: đang chờ lượt renderer 1280×720 để nhận diện chính xác",
                )
            return
        self._farm_renderer_waiting = False
        try:
            detected, _surface, image_size = self.session.detect_farm_state()
            self._farm_capture_blocked_count = 0
            if not self._farm_canvas_is_usable(image_size):
                self._farm_canvas_resize_attempts = (
                    getattr(self, "_farm_canvas_resize_attempts", 0) + 1
                )
                try:
                    resized = self.session.ensure_minimum_game_renderer(
                        *AUTOMATION_RENDERER_WINDOW_SIZE
                    )
                except Exception as error:
                    resized = False
                    self._log_farm(
                        "farm_canvas_resize_error",
                        {
                            "canvas": {"width": image_size[0], "height": image_size[1]},
                            "message": f"{type(error).__name__}: {error}",
                        },
                    )
                self._log_farm(
                    "farm_canvas_too_small",
                    {
                        "canvas": {"width": image_size[0], "height": image_size[1]},
                        "minimum": {
                            "width": FARM_MINIMUM_CANVAS_SIZE[0],
                            "height": FARM_MINIMUM_CANVAS_SIZE[1],
                        },
                        "resize_requested": resized,
                        "attempt": self._farm_canvas_resize_attempts,
                    },
                )
                if resized and self._farm_canvas_resize_attempts <= 3:
                    self._farm_next_at = time.monotonic() + 1.6
                    self._publish(
                        WorkerState.RUNNING,
                        "Auto Farm: khung game quá nhỏ, đang nâng độ phân giải để quét đội chính xác",
                    )
                    return
                # Never interpret a tiny screenshot as an empty team roster.
                # Retrying conservatively is safer than repeatedly searching
                # or selecting a wrong in-game target on a compact tile.
                self._farm_next_at = time.monotonic() + 3.0
                self._publish(
                    WorkerState.RUNNING,
                    "Auto Farm: chờ khung game đủ lớn để nhận diện đội và tài nguyên",
                )
                return
            self._farm_canvas_resize_attempts = 0
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
            target_checkbox_checked = detected.evidence_for(
                FarmTemplateId.BROWSER_SEARCH_TARGET_CHECKBOX_CHECKED
            )
            resource_level_evidence = {
                6: detected.evidence_for(FarmTemplateId.BROWSER_RESOURCE_LEVEL_6),
                7: detected.evidence_for(FarmTemplateId.BROWSER_RESOURCE_LEVEL_7),
            }
            detected_resource_level = next(
                (level for level, evidence in resource_level_evidence.items() if evidence.found),
                None,
            )
            if detected_resource_level is not None:
                self._farm_detected_resource_level = detected_resource_level
                self._farm.set_observed_level(detected_resource_level)
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
            if roster:
                self._farm_roster = roster
                # A readable roster is authoritative even when every row is
                # busy.  Keeping the prior non-empty ready list here made a
                # team that had just departed look ready until the next farm
                # restart.
                self._farm_ready_teams = self._ready_teams_from_roster(roster)
            elif ready_teams:
                # Compatibility for detectors that only return positive Ready
                # slots but cannot yet establish a full roster.
                self._farm_ready_teams = ready_teams
            # A new Farm cycle may only proceed after its completed dispatch
            # has been followed by one readable roster scan. This prevents a
            # stale dashboard/team choice while the previous march changes a
            # row from Ready to Busy.
            if self._farm_post_dispatch_roster_scan_pending:
                if not roster:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(
                        WorkerState.RUNNING,
                        "Auto Farm: chờ quét lại trạng thái đội sau lượt vừa rồi",
                    )
                    return
                self._farm_post_dispatch_roster_scan_pending = False
                self._log_farm(
                    "post_dispatch_roster_scanned",
                    {
                        "roster": [
                            {"team": row.team, "state": row.state.value, "evidence": row.evidence}
                            for row in roster
                        ],
                        "ready_teams": self._farm_ready_teams,
                    },
                )
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto Farm: đã quét lại đội sau lượt; {self._roster_summary(roster)}",
                )
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
                    "target_checkbox_checked": self._evidence_payload(target_checkbox_checked),
                    "resource_level": detected_resource_level,
                    "resource_level_evidence": {
                        str(level): self._evidence_payload(evidence)
                        for level, evidence in resource_level_evidence.items()
                    },
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
            # A relocation that has already reached a verified City owns the
            # next action. Never let the generic preflight see that City and
            # click its normal City→World Map toggle while the intended next
            # control is the dedicated Continent Map icon.
            pending_relocation = self._farm_area_relocation_pending
            if pending_relocation is not None:
                pending_resource, pending_level = pending_relocation
                relocation = self._try_resource_area_relocation(
                    pending_resource,
                    pending_level,
                )
                self._apply_resource_area_relocation_result(
                    relocation,
                    pending_resource,
                    pending_level,
                    reason="resume_verified_city",
                )
                return
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
                        self._retry_farm_or_stop(
                            "search_other_region",
                            "toast yêu cầu tìm ở khu vực khác",
                            screenshot,
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
                elif (
                    state != FarmGameState.RESOURCE_SEARCH
                    and toast_elapsed < FARM_SEARCH_RESULT_SETTLE_SECONDS
                ):
                    self._farm_next_at = time.monotonic() + 0.35
                    self._publish(
                        WorkerState.RUNNING,
                        "Auto Farm: đang chờ popup tài nguyên tải xong sau Tìm kiếm",
                    )
                    return
                elif toast_elapsed < 4.0:
                    self._farm_next_at = time.monotonic() + 0.35
                    self._publish(WorkerState.RUNNING, "Auto Farm: đang quan sát toast hoặc popup sau Tìm kiếm")
                    return
                elif state == FarmGameState.RESOURCE_SEARCH and find_resource.actionable:
                    # The ADB execution service treats a search panel that
                    # remains usable after its observation window as a
                    # bounded no-result attempt.  The website often does not
                    # render a toast for that outcome, so do not stall on the
                    # same Search button or classify it as a technical error.
                    # The same fresh enabled Search control is the confirmed
                    # negative outcome.  Move to an eligible World Map area
                    # now; do not click Search again or rotate blindly.
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
                else:
                    # Neither a result popup nor the same verified Search
                    # panel appeared.  This is a transient UI state, not
                    # permission to edit coordinates.
                    screenshot = self._save_farm_debug_capture("search-result-unverified")
                    self._retry_farm_or_stop(
                        "search_result_unverified",
                        "không xác minh được popup hoặc panel Tìm kiếm sau thao tác",
                        screenshot,
                    )
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
                    input_method = self._click_farm_panel_control(fresh_confirm.bounds, fresh_size)  # type: ignore[arg-type]
                    self._farm_dispatch_click_at = time.monotonic()
                    self._farm_next_at = self._farm_dispatch_click_at + 0.9
                    self._log_farm("confirm_target_resource_expiry", {
                        "bounds": fresh_confirm.bounds,
                        "team": self._farm.team,
                        "input": input_method,
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
                    # team panel disappears. Keep that short observation
                    # window open before accepting the World Map result.
                    elapsed_after_dispatch >= 4.0
                    and self._is_dispatch_postcondition_verified(
                        state=state,
                        team_panel_visible=team_panel.found,
                        team_action_visible=team_action.found,
                        world_map_anchor_visible=(
                            browser_canvas.found
                            or world.found
                            or world_map_coordinate_pin.found
                        ),
                        expected_team=self._farm.team,
                        roster=roster,
                    )
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
                    self._farm_post_dispatch_roster_scan_pending = True
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
                self._retry_farm_or_stop(
                    "dispatch_unverified",
                    "không xác minh được đoàn quân xuất phát sau khi bấm Thu thập",
                    screenshot,
                    team=self._farm.team,
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
                delay = self._world_map_decision_delay(decision, open_search_delay=0.35)
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
                and elapsed_after_click <= FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS
                and not (
                    state == FarmGameState.UNKNOWN
                    and browser_canvas.found
                    and not city.found
                )
            ):
                self._farm_next_at = time.monotonic() + 1.2
                self._publish(
                    WorkerState.RUNNING,
                    "Auto Farm: đang chờ World Map tải xong "
                    f"({elapsed_after_click:.0f}/{FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS:.0f}s)",
                )
                return
            if (
                self._farm_world_map_click_at > 0
                and state == FarmGameState.UNKNOWN
                and elapsed_after_click <= FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS
                and browser_canvas.found
                and not city.found
            ):
                self._farm_world_map_click_at = 0.0
                decision = self._farm.decide(
                    FarmGameState.WORLD_MAP,
                    ready_teams=self._farm_ready_teams,
                )
                delay = self._world_map_decision_delay(decision, open_search_delay=0.8)
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
            if (
                self._farm_world_map_click_at > 0
                and elapsed_after_click > FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS
            ):
                screenshot = self._save_farm_debug_capture("world-map-unverified")
                self._retry_farm_or_stop(
                    "world_map_unverified_after_single_click",
                    "World Map chưa được xác minh sau khi tải",
                    screenshot,
                    elapsed_seconds=round(elapsed_after_click, 2),
                    timeout_seconds=FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS,
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
                        # Retry the verified control at its fixed canvas slot.
                        # Never reuse visual bounds here: the nearby Mail icon
                        # can resemble parts of the castle/parchment artwork.
                        click_bounds = self._map_toggle_layout_bounds(fresh_size)
                        self._click_map_toggle(click_bounds, fresh_size)
                        self._farm_return_city_clicks += 1
                        self._farm_return_city_click_at = time.monotonic()
                        self._farm_next_at = self._farm_return_city_click_at + 1.2
                        self._log_farm(
                            "retry_return_to_city",
                            {
                                "bounds": click_bounds,
                                "matched_bounds": fresh_map_to_city.bounds,
                                "method": "mouse_canvas_ratio",
                                "attempt": self._farm_return_city_clicks,
                                "control": "world_map_castle",
                            },
                        )
                        self._publish(WorkerState.RUNNING, "Auto Farm: đang thử quay về City lần 2")
                        return
                    screenshot = self._save_farm_debug_capture("city-unverified")
                    self._retry_farm_or_stop(
                        "city_unverified_before_cycle",
                        "chưa xác minh được City trước cycle mới",
                        screenshot,
                    )
                    return
            if decision.step == FarmStep.RETURN_TO_CITY and state == FarmGameState.WORLD_MAP:
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_map_to_city = fresh.evidence_for(FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON)
                if fresh.state != DetectedGameState.WORLD_MAP or not fresh_map_to_city.actionable:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: chờ nút City ổn định trước cycle mới")
                    return
                # Detection authorises the transition; the actual input uses
                # the stable bottom-left toggle slot, never matched bounds.
                click_bounds = self._map_toggle_layout_bounds(fresh_size)
                self._click_map_toggle(click_bounds, fresh_size)
                method = "mouse_canvas_ratio"
                self._farm_return_city_click_at = time.monotonic()
                self._farm_return_city_clicks += 1
                self._log_farm(
                    "tap_return_to_city",
                    {
                        "bounds": click_bounds,
                        "matched_bounds": fresh_map_to_city.bounds,
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
                # Search exists only on World Map. Allowing City here made a
                # weak cross-match click a lower-left City control and then
                # restart the whole City -> Map transition.
                and state == FarmGameState.WORLD_MAP
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
                    self._retry_farm_or_stop(
                        "resource_search_unverified",
                        "panel tìm tài nguyên chưa được xác minh sau 2 lần thử",
                        screenshot,
                    )
                    return
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_button = fresh.evidence_for(FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON)
                fresh_world = fresh.evidence_for(FarmTemplateId.WORLD_MAP_ANCHOR)
                fresh_coordinate_pin = fresh.evidence_for(FarmTemplateId.BROWSER_WORLD_MAP_COORDINATE_PIN)
                # A fresh capture may have completed a Map ↔ City transition
                # since the outer check. Never let a magnifier-shaped match
                # on that other screen authorise a lower-left click: the same
                # slot is the Map toggle and would flip back to World Map.
                if fresh.state == DetectedGameState.WORLD_MAP and fresh_button.actionable:
                    click_bounds = fresh_button.bounds
                    method = "resource_search_button"
                elif fresh.state == DetectedGameState.WORLD_MAP and (
                    fresh_world.actionable or fresh_coordinate_pin.actionable
                ):
                    # World Map anchors only authorise input; they are not the
                    # Search control. Always click the measured magnifier slot
                    # instead of reusing an unrelated matched bound.
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
                # This portal's WebGL HUD ignores CDP touch on some profiles.
                # Use an OS-level click first, derived solely from the fresh
                # 1280×720 renderer capture.  If its postcondition is still
                # absent, retry once through Playwright's canvas locator.
                if self._farm_search_clicks == 0:
                    input_method = self._click_farm_native_game_control(click_bounds, fresh_size)
                else:
                    input_method = self._click_farm_panel_control(click_bounds, fresh_size)
                self._farm_search_clicks += 1
                self._farm_next_at = time.monotonic() + 2.0
                self._log_farm(
                    "tap_resource_search",
                    {"bounds": click_bounds, "method": method, "input": input_method},
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
                click_bounds = self._map_toggle_layout_bounds(fresh_size)
                self._click_map_toggle(click_bounds, fresh_size)
                self._farm_city_clicks += 1
                self._farm_world_map_click_at = time.monotonic()
                self._log_farm(
                    "tap_city_to_world_map",
                    {
                        "bounds": click_bounds,
                        "matched_bounds": fresh_city.bounds,
                        "method": "mouse_canvas_ratio",
                    },
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
                    self._retry_farm_or_stop(
                        "gather_unverified",
                        "không xác minh được panel chọn đội sau 2 lần Thu thập",
                        screenshot,
                    )
                    return
                fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                fresh_gather = fresh.evidence_for(FarmTemplateId.BROWSER_GATHER_BUTTON_ENABLED)
                if not fresh_gather.actionable:
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, "Auto Farm: nút Thu thập thay đổi, đang nhận diện lại")
                    return
                input_method = self._click_farm_panel_control(fresh_gather.bounds, fresh_size)  # type: ignore[arg-type]
                self._farm_gather_clicks += 1
                self._farm_next_at = time.monotonic() + 1.5
                self._log_farm(
                    "tap_gather",
                    {"bounds": fresh_gather.bounds, "resource": decision.resource, "level": decision.level, "input": input_method},
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
                    self._retry_farm_or_stop(
                        "team_selection_unverified",
                        f"không xác minh được đội {decision.team} sau 2 lần chọn",
                        screenshot,
                        team=decision.team,
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
                input_method = self._click_farm_panel_control(row_bounds, fresh_size)
                self._farm_expected_team_row = row_bounds
                self._farm_team_selection_clicks += 1
                self._farm_next_at = time.monotonic() + 1.2
                self._log_farm(
                    "tap_expected_team",
                    {
                        "team": decision.team,
                        "badge_bounds": getattr(fresh_badges.get(decision.team), "bounds", None),
                        "row_bounds": row_bounds,
                        "input": input_method,
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
                input_method = self._click_farm_panel_control(fresh_action.bounds, fresh_size)  # type: ignore[arg-type]
                self._farm_dispatch_click_at = time.monotonic()
                self._log_farm(
                    "tap_dispatch",
                    {"team": decision.team, "bounds": fresh_action.bounds, "input": input_method},
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
                # The panel opens on the monster category shown by the red
                # skull button. Always switch through the bottom category
                # button before attempting any of the four resource icons.
                # Previously this happened after resource matching, allowing
                # monster artwork to stall or falsely satisfy a resource
                # template before the intended category was opened.
                if not self._farm_resource_panel_verified:
                    if self._farm_resource_tab_clicked_at <= 0:
                        fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                        fresh_tab = fresh.evidence_for(FarmTemplateId.BROWSER_RESOURCE_TAB_BUTTON)
                        if fresh.state != DetectedGameState.RESOURCE_SEARCH_PANEL:
                            self._farm_next_at = time.monotonic() + 0.5
                            self._publish(WorkerState.RUNNING, "Auto Farm: đang xác minh panel trước khi chọn Tài nguyên")
                            return
                        tab_bounds = (
                            fresh_tab.bounds
                            if fresh_tab.actionable
                            else self._resource_tab_layout_bounds(fresh_size)
                        )
                        method = "template" if fresh_tab.actionable else "verified_panel_layout_fallback"
                        # The category strip is a WebGL game control. A
                        # mouse click can leave the monster tab selected even
                        # when its bounds were exact, while a canvas touch
                        # reliably switches it to the four resources.
                        input_method = self._tap_farm_game_control(tab_bounds, fresh_size)
                        self._farm_resource_tab_clicked_at = time.monotonic()
                        self._farm_next_at = self._farm_resource_tab_clicked_at + 2.0
                        self._log_farm(
                            "tap_resource_tab",
                            {
                                "bounds": tab_bounds,
                                "method": method,
                                "input": input_method,
                                "target": target_resource,
                            },
                        )
                        self._publish(WorkerState.RUNNING, "Auto Farm: đã bấm nút dưới cùng để mở 4 loại tài nguyên")
                        return

                    elapsed = time.monotonic() - self._farm_resource_tab_clicked_at
                    resource_choices_visible = any(
                        evidence.found
                        for evidence in (*resource_buttons.values(), *resource_active_buttons.values())
                    )
                    if not resource_tab.found or resource_choices_visible:
                        self._farm_resource_panel_verified = True
                        self._farm_resource_tab_clicked_at = 0.0
                        self._farm_resource_template_misses = 0
                        self._farm_next_at = time.monotonic() + 0.35
                        self._log_farm(
                            "resource_tab_verified",
                            {
                                "next": "select_resource",
                                "resource_choices_visible": resource_choices_visible,
                            },
                        )
                        self._publish(
                            WorkerState.RUNNING,
                            f"Auto Farm: đã mở 4 loại tài nguyên; đang chọn {decision.resource} cấp {decision.level}",
                        )
                        return
                    if elapsed >= 5.0:
                        screenshot = self._save_farm_debug_capture("resource-tab-unverified")
                        self._retry_farm_or_stop(
                            "resource_tab_unverified",
                            "nút dưới cùng chưa chuyển sang 4 loại tài nguyên",
                            screenshot,
                        )
                        return
                    self._farm_next_at = time.monotonic() + 0.6
                    self._publish(WorkerState.RUNNING, "Auto Farm: đang chờ 4 loại tài nguyên hiển thị")
                    return

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
                    self._click_farm_panel_control(target_resource_button.bounds, image_size)  # type: ignore[arg-type]
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
                    # independently verified search panel. A selected icon is
                    # deliberately recoloured by each city skin, so repeating
                    # the same failed artwork match only delays Search. Once
                    # both the panel and its enabled Search button are fresh,
                    # use the panel-relative slot immediately and verify the
                    # resulting panel state on the next frame.
                    # The panel itself is already fresh and verified.  Do
                    # not let a softer inactive icon or Search-button skin
                    # stall the run indefinitely: after two short polls,
                    # use this resource's fixed panel slot and verify its
                    # supplied active-state template on the next frame.
                    if self._farm_resource_template_misses >= 2:
                        layout_bounds = self._resource_button_layout_bounds(target_resource, image_size)
                        self._click_farm_panel_control(layout_bounds, image_size)
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
                            self._retry_farm_or_stop(
                                "resource_active_unverified",
                                f"chưa xác minh {target_resource} đang được chọn",
                                screenshot,
                                resource=target_resource,
                                level=decision.level,
                                active=self._evidence_payload(target_resource_active),
                            )
                            return
                        self._farm_next_at = time.monotonic() + 0.45
                        self._publish(
                            WorkerState.RUNNING,
                            f"Auto Farm: đang xác minh {target_resource} đã active trước khi bấm Tìm kiếm",
                        )
                        return
                    # The game's search option is opt-in.  Never hit Search
                    # until the supplied checked-state tick is visible on a
                    # freshly matched panel.
                    if not self._farm_target_checkbox_verified:
                        if target_checkbox_checked.found:
                            self._farm_target_checkbox_verified = True
                            self._log_farm("search_target_checkbox_already_checked", {})
                            self._publish(
                                WorkerState.RUNNING,
                                "Auto Farm: checkbox lọc mục tiêu đã được tick, đang tiếp tục tìm tài nguyên",
                            )
                            self._farm_next_at = time.monotonic() + 0.35
                            return
                        if self._farm_target_checkbox_click_at > 0:
                            checkbox_elapsed = time.monotonic() - self._farm_target_checkbox_click_at
                            if (
                                self._farm_target_checkbox_seen_unchecked
                                and target_checkbox_checked.found
                                and find_resource.actionable
                            ):
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
                                self._retry_farm_or_stop(
                                    "search_target_checkbox_unverified",
                                    "chưa xác minh được checkbox lọc mục tiêu trước Tìm kiếm",
                                    screenshot,
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
                            # Search is allowed only after an unchecked state
                            # was actually observed and then changed.  This
                            # prevents a missed template from being mistaken
                            # for a ticked checkbox.
                            self._farm_target_checkbox_seen_unchecked = fresh_checkbox.found
                            self._click_farm_panel_control(checkbox_bounds, fresh_size)
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
                            self._retry_farm_or_stop(
                                "find_resource_unverified",
                                "không xác minh được tài nguyên sau khi tìm 2 lần",
                                screenshot,
                            )
                            return
                        fresh, _fresh_surface, fresh_size = self.session.detect_farm_state()
                        fresh_find_resource = fresh.evidence_for(FarmTemplateId.BROWSER_SEARCH_BUTTON_ENABLED)
                        if fresh_find_resource.actionable:
                            self._click_farm_panel_control(fresh_find_resource.bounds, fresh_size)  # type: ignore[arg-type]
                            self._farm_find_resource_clicks += 1
                            self._farm_find_resource_click_at = time.monotonic()
                            self._farm_next_at = self._farm_find_resource_click_at + 0.6
                            self._log_farm("tap_find_resource", {"bounds": fresh_find_resource.bounds, "resource": decision.resource, "level": decision.level})
                            self._publish(WorkerState.RUNNING, f"Auto Farm: đang tìm {decision.resource} cấp {decision.level}, đang xác minh mục tiêu")
                            return
                    if elapsed >= 5.0:
                        screenshot = self._save_farm_debug_capture("resource-unverified")
                        self._retry_farm_or_stop(
                            "resource_unverified",
                            f"lựa chọn {decision.resource} chưa được xác minh",
                            screenshot,
                            resource=decision.resource,
                        )
                        return
                    self._farm_next_at = time.monotonic() + 0.8
                    self._publish(WorkerState.RUNNING, f"Auto Farm: đang chờ lựa chọn {decision.resource} cập nhật")
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
                self._farm_next_at = time.monotonic() + FARM_NO_READY_TEAM_RESCAN_SECONDS
                self._publish(
                    WorkerState.RUNNING,
                    "Auto Farm: World Map đã xác minh; không có đội sẵn sàng, quét lại sau 2 phút",
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
            self._retry_farm_or_stop(
                "runtime_error",
                str(error),
                screenshot,
                error_type=type(error).__name__,
            )
        except Exception as error:
            screenshot = self._save_farm_debug_capture("error")
            self._retry_farm_or_stop(
                "unexpected_error",
                str(error),
                screenshot,
                error_type=type(error).__name__,
            )
        finally:
            self._release_farm_renderer_when_idle()

    def _retry_farm_or_stop(
        self,
        reason: str,
        message: str,
        screenshot: Path | None = None,
        **details: object,
    ) -> bool:
        """Restart a transient Farm cycle without ending a user-started run."""
        self._farm_recovery_attempts += 1
        attempt = self._farm_recovery_attempts
        backoff_step = min(attempt, FARM_MAX_RECOVERY_ATTEMPTS)
        payload = {
            "reason": reason,
            "attempt": attempt,
            "backoff_step": backoff_step,
            "continuous": True,
            "screenshot": str(screenshot) if screenshot else None,
            **details,
        }
        delay_seconds = FARM_RECOVERY_BASE_DELAY_SECONDS * backoff_step
        self._log_farm("retry", {**payload, "delay_seconds": delay_seconds})
        self._reset_farm_cycle(preserve_recovery_attempts=True)
        self._farm_next_at = time.monotonic() + delay_seconds
        self._publish(
            WorkerState.RUNNING,
            f"Auto Farm: {message}; sẽ tiếp tục thử lại (lần {attempt})",
            f"Thử lại sau {delay_seconds:.0f}s"
            + (f" | Ảnh debug: {screenshot}" if screenshot else ""),
        )
        return True

    def _reset_farm_cycle(self, *, preserve_recovery_attempts: bool = False) -> None:
        """Create a clean farm cycle without stopping the active worker."""
        self._farm = FarmWorkflow()
        if not preserve_recovery_attempts:
            self._farm_recovery_attempts = 0
        self._farm_city_clicks = 0
        self._farm_return_city_click_at = 0.0
        self._farm_return_city_clicks = 0
        self._farm_world_map_click_at = 0.0
        self._farm_ready_teams = ()
        self._farm_roster = ()
        self._farm_post_dispatch_roster_scan_pending = False
        self._farm_search_clicks = 0
        self._farm_resource_tab_clicked_at = 0.0
        self._farm_resource_panel_verified = False
        self._farm_resource_template_misses = 0
        self._farm_resource_selected_at = 0.0
        self._farm_resource_selected_by_layout = False
        self._farm_detected_resource_level = None
        self._farm_target_checkbox_click_at = 0.0
        self._farm_target_checkbox_verified = False
        self._farm_target_checkbox_seen_unchecked = False
        self._farm_target_checkbox_clicks = 0
        self._farm_find_resource_clicks = 0
        self._farm_find_resource_click_at = 0.0
        self._farm_gather_clicks = 0
        self._farm_capture_blocked_count = 0
        self._farm_canvas_resize_attempts = 0
        self._farm_team_selection_clicks = 0
        self._farm_expected_team_row = None
        self._farm_dispatch_click_at = 0.0
        # The coordinate bag belongs to the user-started AutoFarm session,
        # not to one dispatch/recovery cycle. START_FARM creates it and a
        # STOP/START creates the next one. Keeping it here prevents a failed
        # relocation from resetting the bag and selecting the same point
        # forever on every preflight retry.

    def _handle_search_no_result(
        self,
        resource: str,
        level: int,
        *,
        reason: str,
        delay_seconds: float,
        after_tap_seconds: float | None = None,
    ) -> bool:
        """Relocate after a verified Search miss without blind retries.

        The portal's negative outcome is that the same enabled Search button
        remains on the verified resource panel.  In that case the requested
        behaviour is to move the current target to another eligible World Map
        area immediately, not to silently rotate resource types first.
        """
        if self._farm is None:
            return False
        self._log_farm(
            "search_no_result",
            {
                "reason": reason,
                "resource": resource,
                "level": level,
                "after_tap_seconds": round(after_tap_seconds, 2) if after_tap_seconds is not None else None,
            },
        )
        relocation = self._try_resource_area_relocation(resource, level)
        return self._apply_resource_area_relocation_result(
            relocation,
            resource,
            level,
            reason=reason,
        )

    def _apply_resource_area_relocation_result(
        self,
        relocation: str,
        resource: str,
        level: int,
        *,
        reason: str,
    ) -> bool:
        """Keep coordinate relocation separate from the normal map-toggle cycle."""
        if self._farm is None:
            return False
        if relocation == "moved":
            self._farm_area_relocation_pending = None
            self._farm_area_pending_selection = None
            self._farm_resource_selected_at = 0.0
            self._farm_resource_selected_by_layout = False
            self._farm_detected_resource_level = None
            self._farm_resource_template_misses = 0
            self._farm_target_checkbox_click_at = 0.0
            self._farm_target_checkbox_verified = False
            self._farm_target_checkbox_seen_unchecked = False
            self._farm_target_checkbox_clicks = 0
            self._farm_find_resource_clicks = 0
            self._farm_find_resource_click_at = 0.0
            self._log_farm(
                "search_round_area_relocated",
                {"reason": reason, "level": level, "resource": resource},
            )
            self._farm_next_at = time.monotonic() + 1.0
            return True
        if relocation == "map_button_waiting":
            self._farm_area_relocation_pending = (resource, level)
            self._farm_next_at = time.monotonic() + 1.5
            self._log_farm(
                "resource_area_continent_map_waiting",
                {"reason": reason, "level": level, "resource": resource},
            )
            self._publish(
                WorkerState.RUNNING,
                "Auto Farm: đang chờ xác minh icon Map để mở Continent Map",
            )
            return True
        self._farm_area_relocation_pending = None
        self._farm_area_pending_selection = None
        if relocation == "unavailable":
            # Do not continue from a half-verified map/input UI. A new
            # preflight brings the game back to a known state before any next
            # input, while the user-started Farm itself remains running.
            self._log_farm(
                "search_round_area_waiting",
                {"reason": reason, "level": level, "resource": resource},
            )
            return self._retry_farm_or_stop(
                "resource_area_navigation_unverified",
                "không thể xác minh đổi khu vực; quay lại preflight an toàn",
                resource=resource,
                level=level,
            )

        # Three non-repeating points are the whole bounded fallback for this
        # target.  Keep Auto Farm active, but do not loop through coordinates
        # or invent another level after the pool has been exhausted.
        self._farm.step = FarmStep.WAITING
        self._farm.waiting_for_ready_team = False
        self._farm_next_at = time.monotonic() + FARM_NO_READY_TEAM_RESCAN_SECONDS
        self._log_farm(
            "resource_area_pool_unavailable",
            {"reason": reason, "level": level, "resource": resource, "max_attempts": 3},
        )
        self._publish(
            WorkerState.RUNNING,
            f"Auto Farm: không còn điểm khu vực khả dụng cho {resource} cấp {level}; chờ lượt kiểm tra sau",
        )
        return True

    @staticmethod
    def _farm_canvas_is_usable(image_size: tuple[int, int]) -> bool:
        """Return whether a canvas retains enough pixels for Farm vision."""
        width, height = image_size
        minimum_width, minimum_height = FARM_MINIMUM_CANVAS_SIZE
        return width >= minimum_width and height >= minimum_height

    @staticmethod
    def _canvas_ratio_bounds(
        image_size: tuple[int, int],
        *,
        center: tuple[float, float],
        size: tuple[float, float],
        minimum_size: tuple[int, int],
    ) -> tuple[int, int, int, int]:
        """Build screenshot bounds from canvas-relative X/Y ratios.

        The input image is always the latest capture of the current profile's
        game canvas.  No desktop, browser-window, or previous-profile pixels
        participate in the calculation.
        """
        center_x, center_y = center
        width_ratio, height_ratio = size
        if not (0.0 < center_x < 1.0 and 0.0 < center_y < 1.0):
            raise ValueError("Tâm điểm Farm phải là tỉ lệ X/Y trong khoảng 0..1")
        if not (0.0 < width_ratio <= 1.0 and 0.0 < height_ratio <= 1.0):
            raise ValueError("Kích thước vùng Farm phải là tỉ lệ X/Y trong khoảng 0..1")
        image_width, image_height = image_size
        minimum_width, minimum_height = minimum_size
        box_width = min(image_width, max(minimum_width, round(image_width * width_ratio)))
        box_height = min(image_height, max(minimum_height, round(image_height * height_ratio)))
        left = max(0, min(image_width - box_width, round(image_width * center_x - box_width / 2)))
        top = max(0, min(image_height - box_height, round(image_height * center_y - box_height / 2)))
        return (left, top, box_width, box_height)

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
        return ProfileWorker._canvas_ratio_bounds(
            image_size,
            center=FARM_RESOURCE_BUTTON_CENTERS.get(resource, FARM_RESOURCE_BUTTON_CENTERS["food"]),
            size=FARM_RESOURCE_BUTTON_SIZE,
            minimum_size=(36, 36),
        )

    @staticmethod
    def _resource_tab_layout_bounds(image_size: tuple[int, int]) -> tuple[int, int, int, int]:
        """Return the bottom category button of a verified search panel."""
        return ProfileWorker._canvas_ratio_bounds(
            image_size,
            center=FARM_RESOURCE_TAB_CENTER,
            size=FARM_RESOURCE_TAB_SIZE,
            minimum_size=(44, 32),
        )

    @staticmethod
    def _is_dispatch_postcondition_verified(
        *,
        state: FarmGameState,
        team_panel_visible: bool,
        team_action_visible: bool,
        world_map_anchor_visible: bool,
        expected_team: int | None,
        roster: tuple[TeamRosterRow, ...],
    ) -> bool:
        """Verify that a Collect action returned to a stable World Map.

        The generic canvas-ready template is not stable across map themes and
        was false in production even though the coordinate pin and roster were
        both valid. A fresh World Map classification plus disappearance of the
        dispatch panel is the primary invariant. When the roster is readable,
        the selected row must additionally have changed from Ready to Busy.
        """
        if (
            state != FarmGameState.WORLD_MAP
            or team_panel_visible
            or team_action_visible
            or not world_map_anchor_visible
        ):
            return False
        if expected_team is None or not roster:
            return True
        expected_row = next((row for row in roster if row.team == expected_team), None)
        return expected_row is not None and expected_row.state.value == "busy"

    @staticmethod
    def _world_map_search_layout_bounds(
        image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Return the magnifier slot on a verified World Map canvas.

        Browser skins change the magnifier artwork, but retain its HUD slot.
        This is deliberately used only after the World Map coordinate HUD has
        been freshly matched, never as a blind desktop-coordinate click.
        """
        return ProfileWorker._canvas_ratio_bounds(
            image_size,
            center=FARM_WORLD_MAP_SEARCH_CENTER,
            size=FARM_WORLD_MAP_SEARCH_SIZE,
            minimum_size=(38, 36),
        )

    @staticmethod
    def _map_toggle_layout_bounds(
        image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Return the shared City/Map slot from canvas-relative X/Y ratios.

        Detection still verifies the expected screen first. The tap itself
        uses the measured toggle slot so it cannot drift into nearby Mail.
        """
        return ProfileWorker._canvas_ratio_bounds(
            image_size,
            center=FARM_MAP_TOGGLE_CENTER,
            size=FARM_MAP_TOGGLE_SIZE,
            minimum_size=(60, 64),
        )

    def _click_map_toggle(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> None:
        """Click City/Map natively at its exact verified canvas ratio.

        Several portal profiles acknowledge Playwright's synthetic mouse
        call without forwarding it to the WebGL HUD.  The native path uses
        the live window and canvas geometry, so it remains resolution-safe
        while producing the same physical click a user performs.
        """
        if self.session is None:
            raise RuntimeError("Profile chưa mở")
        if not getattr(self, "_automation_renderer_locked", False):
            raise RuntimeError("Nút Map/City chỉ được bấm sau khi renderer đã khóa ở 1280×720")
        native_click = getattr(self.session, "click_game_surface_native_ratio", None)
        click_ratio = getattr(self.session, "click_game_surface_ratio", None)
        if callable(native_click):
            native_click(*FARM_MAP_TOGGLE_CENTER)
        elif callable(click_ratio):
            click_ratio(*FARM_MAP_TOGGLE_CENTER)
        else:
            click_mouse = getattr(self.session, "click_farm_template_mouse", None)
            if callable(click_mouse):
                click_mouse(bounds, image_size)
            else:
                # Compatibility for lightweight test doubles and older sessions.
                self.session.tap_farm_template(bounds, image_size)
        self._automation_renderer_hold_until = (
            time.monotonic() + FARM_MAP_TRANSITION_RENDERER_HOLD_SECONDS
        )

    def _hold_automation_renderer_for_postcondition(self) -> None:
        """Keep the leased 1280×720 canvas through the next control check."""
        self._automation_renderer_hold_until = max(
            getattr(self, "_automation_renderer_hold_until", 0.0),
            time.monotonic() + FARM_CONTROL_POSTCONDITION_HOLD_SECONDS,
        )

    def _click_farm_panel_control(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> str:
        """Click a portal search-panel control on the leased 720p canvas."""
        if self.session is None:
            raise RuntimeError("Profile chưa mở")
        if not getattr(self, "_automation_renderer_locked", False):
            raise RuntimeError("Panel Farm chỉ được bấm sau khi renderer đã khóa ở 1280×720")
        click_mouse = getattr(self.session, "click_farm_template_mouse", None)
        if callable(click_mouse):
            click_mouse(bounds, image_size)
            self._hold_automation_renderer_for_postcondition()
            return "mouse_canvas_template"
        # Compatibility for lightweight tests and older browser sessions.
        self.session.tap_farm_template(bounds, image_size)
        self._hold_automation_renderer_for_postcondition()
        return "touch_template"

    def _tap_farm_game_control(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> str:
        """Tap a verified WebGL game control at its canvas-relative centre."""
        if self.session is None:
            raise RuntimeError("Profile chưa mở")
        if not getattr(self, "_automation_renderer_locked", False):
            raise RuntimeError("Control Farm chỉ được bấm sau khi renderer đã khóa ở 1280×720")
        tap = getattr(self.session, "tap_farm_template", None)
        if callable(tap):
            tap(bounds, image_size)
            self._hold_automation_renderer_for_postcondition()
            return "touch_canvas_template"
        return self._click_farm_panel_control(bounds, image_size)

    def _click_farm_native_game_control(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> str:
        """Use native input only as a second verified WebGL-control attempt."""
        if self.session is None:
            raise RuntimeError("Profile chưa mở")
        if not getattr(self, "_automation_renderer_locked", False):
            raise RuntimeError("Control Farm chỉ được bấm sau khi renderer đã khóa ở 1280×720")
        left, top, width, height = bounds
        image_width, image_height = image_size
        native_click = getattr(self.session, "click_game_surface_native_ratio", None)
        if callable(native_click) and image_width > 0 and image_height > 0:
            native_click((left + width / 2) / image_width, (top + height / 2) / image_height)
            self._hold_automation_renderer_for_postcondition()
            return "native_canvas_ratio"
        return self._tap_farm_game_control(bounds, image_size)

    # Compatibility for tests/integrations that referenced the directional
    # helper before both directions were unified onto the same physical slot.
    _city_to_world_map_layout_bounds = _map_toggle_layout_bounds

    @staticmethod
    def _search_target_checkbox_layout_bounds(
        image_size: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        """Return the checkbox slot within a matcher-verified search panel."""
        return ProfileWorker._canvas_ratio_bounds(
            image_size,
            center=FARM_SEARCH_TARGET_CHECKBOX_CENTER,
            size=FARM_SEARCH_TARGET_CHECKBOX_SIZE,
            minimum_size=(20, 20),
        )

    def _try_resource_area_relocation(self, resource: str, level: int) -> str:
        """Move to one verified map coordinate through the game's map UI.

        All canvas interactions are guarded by a fresh template match. The
        browser adapter opens Continent Map from the verified Map icon exposed
        on World Map (and tolerates a resumed run already on City), then
        requires readable DOM coordinate inputs before it edits either field.
        This keeps the original pair available for a rollback if the
        destination cannot be verified.
        """
        if (
            self.session is None
            or self._farm is None
            or not getattr(self, "_automation_renderer_locked", False)
        ):
            return "unavailable"
        detected, _surface, size = self.session.detect_farm_state()
        if detected.state == DetectedGameState.RESOURCE_SEARCH_PANEL:
            # A no-result Search leaves the bottom panel open. Escape dismisses
            # only this panel; subsequent input is still gated by a fresh,
            # explicit World Map classification.
            self.session.press_escape()
            self._log_farm("close_search_panel_for_area_navigation", {})
            prepared = self._wait_for_farm_detection(
                lambda frame: frame.state == DetectedGameState.WORLD_MAP,
                timeout_seconds=6.0,
            )
            if prepared is None:
                self._log_farm(
                    "resource_area_navigation_blocked",
                    {"reason": "search_panel_close_unverified"},
                )
                return "unavailable"
            detected, _surface, size = prepared

        # The supplied Map icon is present on World Map itself. Returning via
        # the bottom-left castle first changes the HUD to City and removes that
        # control, which was the reason relocation remained stuck. A resumed
        # relocation may already be on City, so accept either state but never
        # click the ambiguous City/World-Map toggle here.
        if detected.state not in {
            DetectedGameState.WORLD_MAP,
            DetectedGameState.CITY,
        }:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "world_map_or_city_unverified",
            })
            return "unavailable"

        # Do not consume a point merely because the Search overlay could not
        # be closed or World Map controls were not verifiable.
        selection = getattr(self, "_farm_area_pending_selection", None)
        if selection is None:
            selection = self._farm_area_selector.next(
                run_id=self._farm_run_id,
                profile_id=self.profile.id,
                resource=resource,
                level=level,
                area_epoch=self._farm_area_epoch,
            )
            self._farm_area_pending_selection = selection
        if selection.exhausted:
            self._log_farm("resource_area_exhausted", {
                "resource": resource, "level": level, "max_attempts": selection.max_attempts,
                "city_levels": selection.city_levels,
            })
            return "exhausted"
        point = selection.point
        assert point is not None
        map_frame, map_size = detected, size
        map_button = map_frame.evidence_for(FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON)
        if not map_button.actionable:
            map_button_result = self._wait_for_farm_detection(
                lambda frame: (
                    frame.state in {
                        DetectedGameState.WORLD_MAP,
                        DetectedGameState.CITY,
                    }
                    and frame.evidence_for(
                        FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON
                    ).actionable
                ),
                timeout_seconds=8.0,
            )
            if map_button_result is None:
                self._log_farm("resource_area_navigation_blocked", {
                    "reason": "continent_map_button_unverified", "point": point,
                    "attempt": selection.attempt,
                })
                return "map_button_waiting"
            map_frame, _surface, map_size = map_button_result
            map_button = map_frame.evidence_for(
                FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON
            )
        input_method = self._tap_farm_game_control(map_button.bounds, map_size)  # type: ignore[arg-type]
        self._log_farm(
            "tap_continent_map_button",
            {
                "bounds": map_button.bounds,
                "point": point,
                "input": input_method,
                "from_state": map_frame.state.value,
            },
        )
        continent_result = self._wait_for_farm_detection(
            lambda frame: frame.state == DetectedGameState.CONTINENT_MAP,
            timeout_seconds=4.0,
        )
        if continent_result is None:
            # WebGL touch can occasionally be ignored. Never retry at stale
            # coordinates: capture again and require the same actionable icon
            # on the same verified map state before using native mouse input.
            retry_frame, _surface, retry_size = self.session.detect_farm_state()
            retry_button = retry_frame.evidence_for(
                FarmTemplateId.BROWSER_CITY_CONTINENT_MAP_BUTTON
            )
            if (
                retry_frame.state in {
                    DetectedGameState.WORLD_MAP,
                    DetectedGameState.CITY,
                }
                and retry_button.actionable
            ):
                input_method = self._click_farm_native_game_control(
                    retry_button.bounds, retry_size  # type: ignore[arg-type]
                )
                self._log_farm(
                    "retry_continent_map_button_native",
                    {
                        "bounds": retry_button.bounds,
                        "point": point,
                        "input": input_method,
                        "from_state": retry_frame.state.value,
                    },
                )
                continent_result = self._wait_for_farm_detection(
                    lambda frame: frame.state == DetectedGameState.CONTINENT_MAP,
                    timeout_seconds=8.0,
                )
        if continent_result is None:
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "continent_map_unverified", "point": point, "attempt": selection.attempt,
            })
            return "unavailable"
        continent, _surface, continent_size = continent_result
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

        def rollback_coordinates(
            frame: object,
            frame_size: tuple[int, int],
            *,
            reason: str,
        ) -> bool:
            """Restore the readable pair; never guess a coordinate field."""
            rollback_pin = frame.evidence_for(FarmTemplateId.CONTINENT_MAP_PIN_BUTTON)  # type: ignore[attr-defined]
            if not rollback_pin.actionable:
                self._log_farm("resource_area_rollback_blocked", {
                    "reason": reason, "point": point, "original": (original_x, original_y),
                })
                return False
            rollback_x, rollback_y = self._coordinate_fields_from_pin(rollback_pin.bounds, frame_size)  # type: ignore[arg-type]
            restored_x = (
                self.session.read_focused_numeric_farm_input(rollback_x, frame_size) is not None
                and self.session.replace_focused_farm_input(original_x)
            )
            restored_y = (
                self.session.read_focused_numeric_farm_input(rollback_y, frame_size) is not None
                and self.session.replace_focused_farm_input(original_y)
            )
            restored = restored_x and restored_y
            self._log_farm("resource_area_rolled_back", {
                "reason": reason, "point": point, "original": (original_x, original_y),
                "verified": restored,
            })
            return restored

        if not self.session.read_focused_numeric_farm_input(x_field, continent_size) == original_x:
            return "unavailable"
        if not self.session.replace_focused_farm_input(point[0]):
            return "unavailable"
        if not self.session.read_focused_numeric_farm_input(y_field, continent_size) == original_y or not self.session.replace_focused_farm_input(point[1]):
            rollback_coordinates(continent, continent_size, reason="y_input_unverified")
            return "unavailable"
        refreshed, _surface, refreshed_size = self.session.detect_farm_state()
        refreshed_pin = refreshed.evidence_for(FarmTemplateId.CONTINENT_MAP_PIN_BUTTON)
        if not refreshed_pin.actionable:
            rollback_coordinates(refreshed, refreshed_size, reason="pin_missing_after_coordinate_input")
            return "unavailable"
        input_method = self._tap_farm_game_control(refreshed_pin.bounds, refreshed_size)  # type: ignore[arg-type]
        self._log_farm("tap_continent_map_pin", {"bounds": refreshed_pin.bounds, "point": point, "input": input_method})
        target_result = self._wait_for_farm_detection(
            lambda frame: frame.evidence_for(FarmTemplateId.CONTINENT_MAP_SEARCH_TARGET_PIN).actionable,
            timeout_seconds=6.0,
        )
        if target_result is None:
            target_frame, _surface, target_size = self.session.detect_farm_state()
            rollback_coordinates(target_frame, target_size, reason="destination_pin_unverified")
            self._log_farm("resource_area_navigation_rolled_back", {
                "reason": "destination_pin_unverified", "point": point,
                "original": (original_x, original_y), "attempt": selection.attempt,
            })
            return "unavailable"
        target_frame, _surface, target_size = target_result
        target_pin = target_frame.evidence_for(FarmTemplateId.CONTINENT_MAP_SEARCH_TARGET_PIN)
        input_method = self._tap_farm_game_control(target_pin.bounds, target_size)  # type: ignore[arg-type]
        self._log_farm("tap_continent_target_pin", {"bounds": target_pin.bounds, "point": point, "input": input_method})
        final_result = self._wait_for_farm_detection(
            lambda frame: (
                frame.state == DetectedGameState.WORLD_MAP
                or (
                    frame.evidence_for(FarmTemplateId.BROWSER_CANVAS_READY_ANCHOR).found
                    and frame.evidence_for(FarmTemplateId.BROWSER_RESOURCE_SEARCH_BUTTON).found
                )
            ),
            timeout_seconds=FARM_WORLD_MAP_LOAD_TIMEOUT_SECONDS,
        )
        if final_result is None:
            final, _surface, _final_size = self.session.detect_farm_state()
            rollback_coordinates(final, _final_size, reason="world_map_unverified_after_target_pin")
            self._log_farm("resource_area_navigation_blocked", {
                "reason": "world_map_unverified_after_target_pin", "point": point,
                "attempt": selection.attempt,
            })
            return "unavailable"
        self._farm.step = FarmStep.OPEN_SEARCH
        # Relocation closes the search panel. The next OPEN_SEARCH must enter
        # the bottom resource category again before continuing the four-type
        # round; a verification from the previous panel is no longer valid.
        self._farm_resource_tab_clicked_at = 0.0
        self._farm_resource_panel_verified = False
        self._farm_area_epoch += 1
        self._log_farm("resource_area_relocated", {
            "resource": resource, "level": level, "point": point,
            "attempt": selection.attempt, "max_attempts": selection.max_attempts,
            "city_levels": selection.city_levels, "original": (original_x, original_y),
        })
        self._publish(WorkerState.RUNNING, f"Auto Farm: đã chuyển tới {point[0]},{point[1]}; mở lại tìm {resource} cấp {level}")
        return "moved"

    def _wait_for_farm_detection(
        self,
        predicate: Callable[[Any], bool],
        *,
        timeout_seconds: float,
        poll_seconds: float = 0.45,
    ) -> tuple[Any, dict[str, float], tuple[int, int]] | None:
        """Wait for a verified game postcondition without issuing input."""
        if self.session is None:
            return None
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            result = self.session.detect_farm_state()
            if predicate(result[0]):
                return result
            if time.monotonic() >= deadline or self.stop_event.is_set():
                return None
            time.sleep(max(0.05, poll_seconds))

    @staticmethod
    def _coordinate_fields_from_pin(
        pin_bounds: tuple[int, int, int, int], image_size: tuple[int, int]
    ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """Port the ADB's pin-relative X/Y input geometry to this canvas."""
        left, top, width, height = pin_bounds
        image_width, image_height = image_size
        center_x = left + width // 2
        center_y = top + height // 2
        x_center = center_x + round(image_width * FARM_COORDINATE_X_FIELD_OFFSET_X_RATIO)
        y_center = center_y + round(image_height * FARM_COORDINATE_Y_FIELD_OFFSET_Y_RATIO)
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

    def _world_map_decision_delay(self, decision: object, *, open_search_delay: float) -> float:
        """Choose the next scan delay without shortening a no-ready wait."""
        if getattr(decision, "step", None) == FarmStep.OPEN_SEARCH:
            return open_search_delay
        if (
            getattr(decision, "step", None) == FarmStep.WAITING
            and self._farm is not None
            and self._farm.waiting_for_ready_team
        ):
            return FARM_NO_READY_TEAM_RESCAN_SECONDS
        return self._farm.policy.retry_delay_seconds if self._farm is not None else 15.0

    @staticmethod
    def _ready_teams_from_roster(roster: tuple[TeamRosterRow, ...]) -> tuple[int, ...]:
        """Derive the current ready set from one complete, readable roster."""
        return tuple(row.team for row in roster if row.state.value == "ready")

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
        """Resolve team 1→4 from the verified panel's fixed visual rows.

        Number templates are retained as diagnostic evidence only. In live
        captures the Team 2 glyph can also match Team 3 at a higher score; if
        its Y coordinate is used for input, the wrong march is selected. The
        panel itself has four stable rows independent of hero art and skin,
        so derive a small non-overlapping target from canvas proportions.
        """
        del cls, badges
        if team not in {1, 2, 3, 4}:
            return None
        image_width, image_height = image_size
        row_left = 0
        row_width = min(image_width, max(48, round(image_width * FARM_TEAM_ROW_WIDTH_RATIO)))
        row_height = min(image_height, max(48, round(image_height * FARM_TEAM_ROW_HEIGHT_RATIO)))
        first_top = max(0, round(image_height * FARM_TEAM_ROW_FIRST_TOP_RATIO))
        row_stride = max(row_height + 1, round(image_height * FARM_TEAM_ROW_STRIDE_RATIO))
        row_top = min(
            max(0, image_height - row_height),
            first_top + (team - 1) * row_stride,
        )
        return row_left, row_top, row_width, row_height

    def _save_farm_debug_capture(self, reason: str) -> Path | None:
        if self.session is None:
            return None
        png = self.session.last_farm_capture_png()
        if not png:
            return None
        folder = self.config.data_dir / "screenshots" / self.profile.id / "farm-debug"
        filename = f"{reason}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        capture = upscale_png_for_diagnostics(png)
        path = write_retained_png(folder / filename, capture, keep=10)

        mirror_root = release_diagnostic_screenshot_directory(self.config.root)
        if mirror_root is not None and mirror_root != self.config.data_dir / "screenshots":
            try:
                mirror_path = write_retained_png(
                    mirror_root / self.profile.id / "farm-debug" / filename,
                    capture,
                    keep=10,
                )
                self._log_farm(
                    "debug_screenshot_mirrored",
                    {"reason": reason, "path": str(mirror_path)},
                )
            except OSError as error:
                # A moved release may no longer have access to its build
                # source.  The regular runtime capture above must still be
                # available and Farm must not fail merely because mirroring
                # is unavailable.
                self._log_farm(
                    "debug_screenshot_mirror_error",
                    {"reason": reason, "path": str(mirror_root), "error": str(error)},
                )
        return path

    def _close_session(self, *, close_browser: bool = False) -> None:
        self._farm = None
        self._farm_renderer_waiting = False
        # Closing does not need to redraw the compact tile, but it must free
        # the shared high-resolution lease for the next profile.
        self._release_automation_renderer(restore=False)
        if self.session is None:
            return
        try:
            self.session.close(close_browser=close_browser)
        finally:
            self.session = None

    def _handle_external_close(self) -> None:
        self._farm = None
        self._farm_renderer_waiting = False
        self._release_automation_renderer(restore=False)
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
        self.sync_target_ids: set[str] = set()
        self.drag_items_visible = False
        self.scrollbars_visible = False
        self.windows_topmost = False
        self._resource_cpu_samples: dict[str, tuple[float, float]] = {}
        self._sync_lock = threading.Lock()
        # A compact 5-column grid must never create five concurrent 720p
        # WebGL surfaces. Workers lease this lock only while real pixels are
        # required for Farm or mail recognition.
        self._automation_renderer_lock = threading.Lock()
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
                    automation_renderer_lock=self._automation_renderer_lock,
                )

    def submit(self, profile_id: str, kind: CommandKind, **payload: object) -> None:
        self.workers[profile_id].submit(WorkerCommand(kind, dict(payload)))

    def enable_mail_monitor(self, profile_ids: set[str]) -> None:
        for profile_id in profile_ids:
            worker = self.workers.get(profile_id)
            if worker is not None:
                worker.enable_mail_monitor()

    def cancel_mail_monitor(self, profile_ids: set[str] | None = None) -> None:
        targets = self.workers if profile_ids is None else {
            profile_id: self.workers[profile_id]
            for profile_id in profile_ids
            if profile_id in self.workers
        }
        for worker in targets.values():
            worker.cancel_mail_monitor()

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

    def enable_sync(self, master_id: str, target_ids: set[str] | None = None) -> None:
        if master_id not in self.workers:
            raise KeyError(f"Không tìm thấy profile master: {master_id}")
        targets = (
            {profile_id for profile_id in self.workers if profile_id != master_id}
            if target_ids is None
            else {profile_id for profile_id in target_ids if profile_id in self.workers and profile_id != master_id}
        )
        if not targets:
            raise ValueError("Hãy chọn ít nhất một profile nhận đồng bộ")
        with self._sync_lock:
            self.sync_enabled = True
            self.sync_master_id = master_id
            self.sync_target_ids = targets
        self.event_log.write(
            "sync_enabled",
            {"master_profile_id": master_id, "target_profile_ids": sorted(targets)},
        )
        for profile_id in self.workers:
            self.submit(
                profile_id,
                CommandKind.SET_SYNC_SOURCE,
                enabled=profile_id == master_id,
            )

    def disable_sync(self) -> None:
        with self._sync_lock:
            was_enabled = self.sync_enabled
            previous_master = self.sync_master_id
            self.sync_enabled = False
            self.sync_master_id = None
            self.sync_target_ids.clear()
        if was_enabled:
            self.event_log.write(
                "sync_disabled",
                {"master_profile_id": previous_master},
            )
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

    def toggle_all_profile_windows(self) -> tuple[bool, int]:
        """Minimize all open profile windows, or restore them when all are minimized.

        The returned boolean is the resulting minimized state. Reading the
        native state on every click keeps the toggle correct when a user has
        manually minimized or restored individual Chrome windows.
        """
        handles: list[int] = []
        for profile in self.config.profiles:
            worker = self.workers.get(profile.id)
            session = worker.session if worker else None
            if session is None:
                continue
            hwnd = session.window_handle
            if hwnd is not None:
                handles.append(hwnd)
        if not handles:
            return False, 0
        minimize = not all(is_window_minimized(hwnd) for hwnd in handles)
        changed = sum(
            1 for hwnd in handles if set_window_minimized(hwnd, minimize)
        )
        return minimize, changed

    def restore_profile_windows(self, profile_ids: set[str] | None = None) -> int:
        """Restore only minimized profile windows without changing geometry."""
        restored = 0
        for profile in self.config.profiles:
            if profile_ids is not None and profile.id not in profile_ids:
                continue
            worker = self.workers.get(profile.id)
            session = worker.session if worker else None
            if session is None:
                continue
            hwnd = session.window_handle
            if hwnd is None or not is_window_minimized(hwnd):
                continue
            if set_window_minimized(hwnd, False):
                restored += 1
        return restored

    def arrange_windows(
        self,
        columns_per_row: int | None = None,
        *,
        profile_ids: set[str] | None = None,
        layout_profile_ids: set[str] | None = None,
    ) -> int:
        """Tile requested profiles, optionally reserving a stable final grid."""
        if columns_per_row is not None and not 1 <= int(columns_per_row) <= 6:
            raise ValueError("Số cửa sổ mỗi hàng phải từ 1 đến 6")
        opened: dict[str, tuple[str, int, WindowRect, WindowRect]] = {}
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
            opened[profile.id] = (profile.id, hwnd, outer, visible)
        if not opened:
            return 0
        layout_ids = [
            profile.id
            for profile in self.config.profiles
            if (
                profile.id in opened
                if layout_profile_ids is None
                else profile.id in layout_profile_ids
            )
        ]
        if not layout_ids:
            layout_ids = list(opened)
        columns = int(columns_per_row or len(layout_ids))
        work_areas = get_monitor_work_areas()
        monitor_count = min(len(work_areas), len(layout_ids))
        base_count, remainder = divmod(len(layout_ids), monitor_count)
        layouts: list[tuple[tuple[str, int, WindowRect, WindowRect], tuple[int, int], int, int]] = []
        offset = 0
        for monitor_index, work_area in enumerate(work_areas[:monitor_count]):
            profile_count = base_count + (1 if monitor_index < remainder else 0)
            monitor_profile_ids = layout_ids[offset : offset + profile_count]
            offset += profile_count
            rows = max(1, (profile_count + columns - 1) // columns)
            # Keep every game surface at 16:9 while fitting the selected grid
            # independently inside each monitor's work area.
            max_width_by_columns = max(1, work_area.width // columns)
            max_width_by_rows = max(1, (work_area.height // rows) * 16 // 9)
            visible_width = min(max_width_by_columns, max_width_by_rows)
            visible_height = max(1, visible_width * 9 // 16)
            positions = calculate_tiled_positions(
                work_area,
                visible_width,
                visible_height,
                profile_count,
                columns_per_row=columns,
            )
            for profile_id, position in zip(
                monitor_profile_ids, positions, strict=True
            ):
                profile = opened.get(profile_id)
                if profile is not None:
                    layouts.append(
                        (profile, position, visible_width, visible_height)
                    )
        moved = 0
        resized = 0
        pending_resizes = sum(
            1
            for (_profile, _position, target_width, target_height) in layouts
            if (
                abs(_profile[2].width - (target_width + (_profile[2].width - _profile[3].width))) > 2
                or abs(_profile[2].height - (target_height + (_profile[2].height - _profile[3].height))) > 2
            )
        )
        stagger_resizes = pending_resizes > 10
        for (profile_id, _hwnd, outer, visible), (x, y), visible_width, visible_height in layouts:
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
                target_x = x - (visible.left - outer.left)
                target_y = y - (visible.top - outer.top)
                needs_resize = (
                    abs(outer.width - frame_width) > 2
                    or abs(outer.height - frame_height) > 2
                )
                needs_move = (
                    abs(outer.left - target_x) > 2
                    or abs(outer.top - target_y) > 2
                )
                if not needs_move and not needs_resize:
                    moved += 1
                    continue
                move_window_outer(
                    _hwnd,
                    target_x,
                    target_y,
                    frame_width,
                    frame_height,
                    topmost=self.windows_topmost,
                    resize=needs_resize,
                )
                moved += 1
                if stagger_resizes and needs_resize:
                    resized += 1
                    # WebGL canvases redraw after native resize. Throttle
                    # large layouts so 45+ Chrome profiles do not submit all
                    # texture reallocations to the display driver at once.
                    time.sleep(0.12)
                    if resized % 5 == 0 and resized < pending_resizes:
                        time.sleep(1.25)
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
            target_ids = set(self.sync_target_ids)
        if not enabled or source_profile_id != master_id:
            return
        delivered = 0
        for profile_id, worker in self.workers.items():
            if profile_id not in target_ids or worker.session is None:
                continue
            worker.submit(WorkerCommand(CommandKind.SYNC_INPUT, {"event": event}))
            delivered += 1
        event_type = str(event.get("type", ""))
        if event_type in {"pointerdown", "pointerup", "keydown", "keyup"}:
            self.event_log.write(
                "sync_input_dispatched",
                {
                    "master_profile_id": source_profile_id,
                    "type": event_type,
                    "target_count": delivered,
                },
            )

    def stop_all(self) -> None:
        for worker in self.workers.values():
            worker.stop()

    def shutdown(self) -> None:
        self.disable_sync()
        for worker in self.workers.values():
            worker.shutdown()
        for worker in self.workers.values():
            worker.join()
