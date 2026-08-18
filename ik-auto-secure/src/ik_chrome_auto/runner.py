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
from ik_chrome_auto.game2048 import Auto2048Player, board_text
from ik_chrome_auto.models import (
    AppConfig,
    Auto2048Speed,
    CommandKind,
    ProfileConfig,
    WorkerCommand,
    WorkerSnapshot,
    WorkerState,
)
from ik_chrome_auto.reader import redact
from ik_chrome_auto.storage import write_retained_png
from ik_chrome_auto.windows import (
    calculate_tiled_positions,
    get_visible_window_rect,
    get_window_rect,
    get_window_process_tree_usage,
    get_work_area,
    snapshot_process_parents,
    trim_window_process_tree,
    WindowRect,
)

UpdateCallback = Callable[[WorkerSnapshot], None]
InputCallback = Callable[[str, dict[str, object]], None]
CoordinateCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class Auto2048Timing:
    label: str
    move_delay_seconds: float
    pending_delay_seconds: float


AUTO_2048_TIMINGS = {
    Auto2048Speed.SAFE: Auto2048Timing("An toàn", 1.20, 0.50),
    Auto2048Speed.BALANCED: Auto2048Timing("Cân bằng", 0.80, 0.35),
    Auto2048Speed.FAST: Auto2048Timing("Nhanh", 0.55, 0.25),
    Auto2048Speed.TURBO: Auto2048Timing("Turbo", 0.35, 0.18),
}

AUTO_2048_TARGET_LEVEL = 12


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
        drag_item_visible: bool = True,
        topmost: bool = False,
        auto_2048_speed: Auto2048Speed = Auto2048Speed.BALANCED,
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
        self.stop_event = threading.Event()
        self.commands: queue.Queue[WorkerCommand] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.session: ChromeProfileSession | None = None
        self._thread_lock = threading.Lock()
        self._sync_source_enabled = False
        self._inspector_enabled = False
        self._drag_item_visible = drag_item_visible
        self._topmost = topmost
        self._auto_2048: Auto2048Player | None = None
        self._auto_2048_next_at = 0.0
        self._auto_2048_errors = 0
        self._auto_2048_speed = auto_2048_speed

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
        snapshot = WorkerSnapshot(self.profile.id, state, message, detail)
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
                    elif self._auto_2048 is not None and time.monotonic() >= self._auto_2048_next_at:
                        self._run_2048_tick()
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
                if command.kind == CommandKind.START_2048:
                    if self.session is None:
                        self._publish(WorkerState.STARTING, "Đang mở profile cho Auto 2048")
                        self._ensure_session(navigate=True)
                    self._auto_2048 = Auto2048Player()
                    self._auto_2048_errors = 0
                    self._auto_2048_next_at = 0.0
                    timing = AUTO_2048_TIMINGS[self._auto_2048_speed]
                    self._publish(
                        WorkerState.RUNNING,
                        f"Auto 2048 Smart đã bật | tốc độ={timing.label}",
                    )
                    continue
                if command.kind == CommandKind.STOP_2048:
                    self._auto_2048 = None
                    self._publish(
                        WorkerState.READY if self.session is not None else WorkerState.STOPPED,
                        "Đã dừng Auto 2048",
                    )
                    continue
                if command.kind == CommandKind.SET_2048_SPEED:
                    self._auto_2048_speed = Auto2048Speed(
                        str(command.payload.get("speed", Auto2048Speed.BALANCED.value))
                    )
                    if self._auto_2048 is not None:
                        timing = AUTO_2048_TIMINGS[self._auto_2048_speed]
                        self._publish(
                            WorkerState.RUNNING,
                            f"Đã đổi tốc độ Auto 2048: {timing.label}",
                        )
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

    def _run_2048_tick(self) -> None:
        if self.session is None or self._auto_2048 is None:
            return
        png: bytes | None = None
        timing = AUTO_2048_TIMINGS[self._auto_2048_speed]
        try:
            png, _surface = self.session.capture_game_surface_png()
            decision = self._auto_2048.plan(png)
            maximum = max(value for row in decision.scan.board for value in row)
            if maximum >= AUTO_2048_TARGET_LEVEL:
                self._auto_2048 = None
                self._publish(
                    WorkerState.COMPLETED,
                    f"Auto 2048 đã dừng khi đạt level {AUTO_2048_TARGET_LEVEL}",
                    board_text(decision.scan.board),
                )
                return
            if decision.waiting:
                self._auto_2048_next_at = (
                    time.monotonic() + timing.pending_delay_seconds
                )
                if self._auto_2048.stale_retries == 1:
                    self._publish(
                        WorkerState.RUNNING,
                        "Auto 2048 đang chờ bàn cập nhật; không gửi lặp touch",
                        board_text(decision.scan.board),
                    )
                return
            if decision.direction is None:
                self._auto_2048 = None
                self._publish(
                    WorkerState.COMPLETED,
                    f"2048 kết thúc; ô cao nhất={maximum}",
                    board_text(decision.scan.board),
                )
                return
            self.session.swipe_game_surface(
                decision.direction,
                decision.scan.grid.box,
                (decision.scan.image_width, decision.scan.image_height),
            )
            self._auto_2048_errors = 0
            self._auto_2048_next_at = time.monotonic() + timing.move_delay_seconds
            arrows = {"left": "←", "right": "→", "up": "↑", "down": "↓"}
            self._publish(
                WorkerState.RUNNING,
                (
                    f"Auto 2048 {arrows[decision.direction]} | max={maximum} "
                    + f"| AI depth={decision.depth} "
                    + f"| tin cậy={decision.scan.confidence:.0%} "
                    + f"| tốc độ={timing.label}"
                ),
                board_text(decision.scan.board),
            )
        except Exception as error:
            self._auto_2048_errors += 1
            self._auto_2048_next_at = time.monotonic() + 1.0
            debug_path: Path | None = None
            if png is not None and self._auto_2048_errors in {1, 6}:
                debug_dir = self.config.data_dir / "screenshots" / self.profile.id
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_path = debug_dir / (
                    f"2048-debug-{time.strftime('%Y%m%d-%H%M%S')}.png"
                )
                write_retained_png(debug_path, png, keep=2)
            if self._auto_2048_errors >= 6:
                self._auto_2048 = None
                self._publish(
                    WorkerState.ERROR,
                    f"Auto 2048 đã dừng: {error}",
                    (
                        f"{type(error).__name__}: {error}"
                        + (f" | ảnh debug: {debug_path}" if debug_path else "")
                    ),
                )
            elif self._auto_2048_errors == 1:
                self._publish(
                    WorkerState.RUNNING,
                    f"Auto 2048 đang chờ nhận dạng ({self._auto_2048_errors}/6): {error}",
                    f"Ảnh debug: {debug_path}" if debug_path else "",
                )

    def _close_session(self) -> None:
        self._auto_2048 = None
        if self.session is None:
            return
        try:
            self.session.close()
        finally:
            self.session = None

    def _handle_external_close(self) -> None:
        self._auto_2048 = None
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
        self.drag_items_visible = True
        self.windows_topmost = False
        self.auto_2048_speed = config.auto_2048_speed
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
                    topmost=self.windows_topmost,
                    auto_2048_speed=self.auto_2048_speed,
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

    def set_auto_2048_speed(self, speed: Auto2048Speed) -> None:
        self.auto_2048_speed = Auto2048Speed(speed)
        self.config.auto_2048_speed = self.auto_2048_speed
        for profile in self.config.profiles:
            if profile.id in self.workers:
                self.submit(
                    profile.id,
                    CommandKind.SET_2048_SPEED,
                    speed=self.auto_2048_speed.value,
                )

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

    def arrange_windows(self, columns_per_row: int | None = None) -> int:
        if columns_per_row is not None and not 2 <= int(columns_per_row) <= 6:
            raise ValueError("Số cửa sổ mỗi hàng phải từ 2 đến 6")
        opened: list[tuple[str, int, WindowRect, WindowRect]] = []
        for profile in self.config.profiles:
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
        window_width = max(item[3].width for item in opened)
        window_height = max(item[3].height for item in opened)
        positions = calculate_tiled_positions(
            get_work_area(),
            window_width,
            window_height,
            len(opened),
            columns_per_row=columns_per_row,
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
                # Window placement is a native Win32 operation.  Apply it now
                # instead of queuing behind browser work so the dashboard's
                # "Áp dụng sắp xếp" button updates every running Chrome at once.
                session.move_window(
                    x - (visible.left - outer.left),
                    y - (visible.top - outer.top),
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
