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


class Auto2048Speed(StrEnum):
    SAFE = "safe"
    BALANCED = "balanced"
    FAST = "fast"
    TURBO = "turbo"


class CommandKind(StrEnum):
    OPEN = "open"
    READ = "read"
    SCREENSHOT = "screenshot"
    RESIZE = "resize"
    SYNC_INPUT = "sync_input"
    SET_SYNC_SOURCE = "set_sync_source"
    SET_INSPECTOR = "set_inspector"
    SET_DRAG_ITEM = "set_drag_item"
    MOVE_WINDOW = "move_window"
    SET_TOPMOST = "set_topmost"
    START_2048 = "start_2048"
    STOP_2048 = "stop_2048"
    SET_2048_SPEED = "set_2048_speed"
    STOP = "stop"
    SHUTDOWN = "shutdown"


@dataclass(slots=True)
class BrowserSettings:
    chrome_executable: str = "auto"
    headless: bool = False
    app_mode: bool = True
    profile_title: bool = True
    low_memory_mode: bool = True
    auto_resize: bool = True
    viewport_width: int = 500
    viewport_height: int = 300
    windows_per_row: int = 3
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
    auto_2048_speed: Auto2048Speed = Auto2048Speed.BALANCED
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


@dataclass(slots=True)
class WorkerCommand:
    kind: CommandKind
    payload: dict[str, Any] = field(default_factory=dict)
