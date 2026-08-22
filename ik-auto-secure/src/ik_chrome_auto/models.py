from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ProfileMode(StrEnum):
    MANAGED = "managed"
    CDP = "cdp"


class WorkerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class CommandKind(StrEnum):
    OPEN = "open"
    READ = "read"
    SCREENSHOT = "screenshot"
    RESIZE = "resize"
    SYNC_INPUT = "sync_input"
    SET_SYNC_SOURCE = "set_sync_source"
    SET_INSPECTOR = "set_inspector"
    SET_DRAG_ITEM = "set_drag_item"
    SET_SCROLLBARS = "set_scrollbars"
    MOVE_WINDOW = "move_window"
    SET_TOPMOST = "set_topmost"
    START_FARM = "start_farm"
    STOP_FARM = "stop_farm"
    STOP = "stop"
    SHUTDOWN = "shutdown"


@dataclass(slots=True)
class BrowserSettings:
    chrome_executable: str = "auto"
    headless: bool = False
    app_mode: bool = True
    profile_title: bool = True
    low_memory_mode: bool = True
    low_gpu_mode: bool = True
    render_fps_limit: int = 24
    auto_resize: bool = True
    viewport_width: int = 500
    viewport_height: int = 281
    windows_per_row: int = 6
    slow_mo_ms: int = 0
    startup_timeout_ms: int = 90_000


@dataclass(slots=True)
class CaptureSettings:
    allowed_hosts: tuple[str, ...] = (
        "ik.playfun.vn",
        "gtarcade.com",
        "smobgame.com",
    )
    max_body_bytes: int = 131_072
    max_text_chars: int = 6_000
    # Payload capture can contain account data. It is deliberately opt-in.
    capture_response_bodies: bool = False
    network_capture_enabled: bool = False
    snapshot_retention: int = 50


@dataclass(slots=True)
class ProfileConfig:
    id: str
    name: str
    mode: ProfileMode = ProfileMode.MANAGED
    user_data_dir: Path | None = None
    cdp_url: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class AppConfig:
    root: Path
    source: Path
    target_url: str
    data_dir: Path
    browser: BrowserSettings
    capture: CaptureSettings
    profiles: list[ProfileConfig] = field(default_factory=list)

    def profile(self, profile_id: str) -> ProfileConfig:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise KeyError(f"Không tìm thấy profile: {profile_id}")


@dataclass(slots=True)
class WorkerSnapshot:
    profile_id: str
    state: WorkerState = WorkerState.STOPPED
    message: str = "Đã dừng"
    detail: str = ""
    # Latest World Map roster read: (team number, ready|busy). It is display
    # metadata only and never authorises an automation input by itself.
    farm_roster: tuple[tuple[int, str], ...] = ()


@dataclass(slots=True)
class WorkerCommand:
    kind: CommandKind
    payload: dict[str, Any] = field(default_factory=dict)
