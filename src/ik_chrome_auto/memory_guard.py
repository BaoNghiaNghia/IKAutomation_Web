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


def guard_new_chrome_launch(snapshot: CommitSnapshot | None = None) -> None:
    """Fail closed only when launching NEW Chrome, not when reattaching CDP."""
    current = snapshot if snapshot is not None else get_windows_commit_snapshot()
    if current is None or not current.critical:
        return
    available = current.available_bytes / GIB
    used_pct = current.usage_ratio * 100
    raise RuntimeError(
        f"Windows Commit Memory sap het ({used_pct:.1f}% da dung, "
        f"con {available:.2f} GiB). Khong mo them Chrome de tranh crash. "
        "Tang paging file tren o D:, khoi dong lai Windows khi da luu "
        "cac phien game. Profile Chrome dang mo duoc giu nguyen."
    )