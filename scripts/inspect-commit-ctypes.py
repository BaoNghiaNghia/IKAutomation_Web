"""Read-only memory commit snapshot for Chrome crash analysis."""
from __future__ import annotations
import ctypes
import json
from ctypes import wintypes

class Status(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

status = Status()
status.dwLength = ctypes.sizeof(status)
if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
    raise OSError("GlobalMemoryStatusEx failed")
gib=1024**3
total = status.ullTotalPageFile
used=total-status.ullAvailPageFile
print(json.dumps({
    "physical_total_gib": round(status.ullTotalPhys/gib, 2),
    "physical_free_gib": round(status.ullAvailPhys/gib, 2),
    "commit_limit_gib":round(total/gib,2),
    "commit_used_gib":round(used/gib,2),
    "commit_free_gib":round(status.ullAvailPageFile/gib,2),
    "commit_percent":round(100*used/total,1) if total else 0,
}))