"""Guard new Chrome launches against exhausted Windows commit memory.

The Windows commit limit equals physical RAM plus configured paging files.
Physical RAM availability alone is NOT a safe resource-headroom metric.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

GIB = 1024**3
MIN_COMMIT_HEADROOM_BYTES = 4 * GIB
MAX_COMMIT_USAGE_RATIO = 0.90


@dataclass(frozen=True)
class CommitSnapshot:
    limit_bytes: int
    available_bytes: int

    @property
    def usage_ratio(self) -> float:
        return 1 - self.available_bytes / max(self.limit_bytes, 1)

    @property
    def critical(self) -> bool:
        return (
            self.available_bytes < MIN_COMMIT_HEADROOM_BYTES
            or self.usage_ratio >= MAX_COMMIT_USAGE_RATIO
        )


def get_windows_commit_snapshot() -> CommitSnapshot | None:
    if sys.platform != "win32":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("Cannot verify Windows commit headroom")
    return CommitSnapshot(
        limit_bytes=int(status.ullTotalPageFile),
        available_bytes=int(status.ullAvailPageFile),
    )


def guard_physical_ram_for_new_chrome_launch(
    *, total_bytes: int, available_bytes: int, load_percent: float
) -> None:
    """Second hard gate against physical-RAM exhaustion, even with pagefile space."""
    from ik_chrome_auto.farm_launch_policy import FarmLaunchPolicy

    policy = FarmLaunchPolicy.for_total_memory(total_bytes)
    reason = policy.hard_ram_pause_reason(
        available_memory_bytes=available_bytes, memory_load_percent=load_percent
    )
    if reason:
        raise RuntimeError(
            f"Physical RAM khong du de mo Chrome moi ({reason}). "
            "Khong mo them Chrome; profile dang mo duoc giu nguyen."
        )


def guard_new_chrome_launch(snapshot: CommitSnapshot | None = None) -> None:
    """Fail closed for new Chrome; verified CDP reattach bypasses this function."""
    current = snapshot if snapshot is not None else get_windows_commit_snapshot()
    if current is not None and current.critical:
        available = current.available_bytes / GIB
        used_pct = current.usage_ratio * 100
        raise RuntimeError(
            f"Windows Commit Memory sap het ({used_pct:.1f}% da dung, "
            f"con {available:.2f} GiB). Khong mo them Chrome de tranh crash. "
            "Tang paging file tren o D:, khoi dong lai Windows khi da luu "
            "cac phien game. Profile Chrome dang mo duoc giu nguyen."
        )
    # Explicit synthetic commit snapshots are used by unit tests.
    # Live Chrome starts must also validate physical RAM just before Popen.
    if snapshot is None and sys.platform == "win32":
        try:
            from ik_chrome_auto.windows import get_system_memory_status
            memory = get_system_memory_status()
            guard_physical_ram_for_new_chrome_launch(
                total_bytes=memory.total_bytes,
                available_bytes=memory.available_bytes,
                load_percent=memory.load_percent,
            )
        except Exception as error:
            raise RuntimeError(
                "Khong xac minh duoc RAM. Khong mo Chrome moi de tranh treo."
            ) from error