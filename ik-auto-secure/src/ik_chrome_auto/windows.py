from __future__ import annotations

import ctypes
import struct
import sys
import uuid
import zlib
from ctypes import wintypes
from dataclasses import dataclass


TASKBAR_APP_ID = "IKChromeAuto.Profiles"


@dataclass(frozen=True, slots=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class ProcessResourceUsage:
    process_ids: tuple[int, ...] = ()
    working_set_bytes: int = 0
    cpu_seconds: float = 0.0

    @property
    def process_count(self) -> int:
        return len(self.process_ids)


def calculate_tiled_positions(
    work_area: WindowRect,
    window_width: int,
    window_height: int,
    count: int,
    *,
    gap: int = 0,
    columns_per_row: int | None = None,
) -> list[tuple[int, int]]:
    """Return left-to-right, top-to-bottom positions inside the desktop work area."""
    if count <= 0:
        return []
    width = max(1, int(window_width))
    height = max(1, int(window_height))
    columns = (
        max(1, int(columns_per_row))
        if columns_per_row is not None
        else max(1, (work_area.width + gap) // (width + gap))
    )
    rows = max(1, (work_area.height + gap) // (height + gap))
    capacity = max(1, columns * rows)
    positions: list[tuple[int, int]] = []
    for index in range(count):
        slot = index % capacity
        page = index // capacity
        row, column = divmod(slot, columns)
        # If more windows exist than the screen can hold at the requested size,
        # cascade later pages slightly so every title bar remains selectable.
        cascade = page * 28
        x = min(
            work_area.right - width,
            work_area.left + column * (width + gap) + cascade,
        )
        y = min(
            work_area.bottom - height,
            work_area.top + row * (height + gap) + cascade,
        )
        positions.append((max(work_area.left, x), max(work_area.top, y)))
    return positions


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", _BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    _gdi32.BitBlt.restype = wintypes.BOOL
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESSENTRY32W),
    ]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESSENTRY32W),
    ]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    _psapi.EmptyWorkingSet.restype = wintypes.BOOL

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_string(cls, value: str) -> _GUID:
            raw = uuid.UUID(value).bytes_le
            return cls.from_buffer_copy(raw)

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]

    class _PROPVARIANT_VALUE(ctypes.Union):
        _fields_ = [("pwszVal", ctypes.c_wchar_p), ("ullVal", ctypes.c_ulonglong)]

    class _PROPVARIANT(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("value", _PROPVARIANT_VALUE),
        ]

    _IID_IPROPERTY_STORE = _GUID.from_string("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
    _PKEY_APP_USER_MODEL_ID = _PROPERTYKEY(
        _GUID.from_string("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
        5,
    )


def is_window(hwnd: int | None) -> bool:
    return bool(sys.platform == "win32" and hwnd and _user32.IsWindow(wintypes.HWND(hwnd)))


def descendant_process_ids(
    root_pid: int,
    parent_by_pid: dict[int, int],
) -> tuple[int, ...]:
    """Return a stable breadth-first process tree, including ``root_pid``."""
    result: list[int] = []
    pending = [int(root_pid)]
    seen: set[int] = set()
    while pending:
        pid = pending.pop(0)
        if pid <= 0 or pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        pending.extend(
            child
            for child, parent in parent_by_pid.items()
            if parent == pid and child not in seen
        )
    return tuple(result)


def snapshot_process_parents() -> dict[int, int]:
    if sys.platform != "win32":
        return {}
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid:
        return {}
    parents: dict[int, int] = {}
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        available = bool(_kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            available = bool(_kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        _kernel32.CloseHandle(snapshot)
    return parents


def _filetime_seconds(value: wintypes.FILETIME) -> float:
    ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
    return ticks / 10_000_000.0


def get_window_process_tree_usage(
    hwnd: int,
    parent_by_pid: dict[int, int] | None = None,
) -> ProcessResourceUsage:
    """Measure working-set RAM and cumulative CPU for a Chrome window tree."""
    if sys.platform != "win32" or not is_window(hwnd):
        return ProcessResourceUsage()
    root = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(root))
    if not root.value:
        return ProcessResourceUsage()
    parents = parent_by_pid if parent_by_pid is not None else snapshot_process_parents()
    pids = descendant_process_ids(int(root.value), parents)
    measured: list[int] = []
    working_set = 0
    cpu_seconds = 0.0
    for pid in pids:
        handle = _kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            memory = _PROCESS_MEMORY_COUNTERS()
            memory.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
            if _psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(memory),
                memory.cb,
            ):
                working_set += int(memory.WorkingSetSize)
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                cpu_seconds += _filetime_seconds(kernel) + _filetime_seconds(user)
            measured.append(pid)
        finally:
            _kernel32.CloseHandle(handle)
    return ProcessResourceUsage(tuple(measured), working_set, cpu_seconds)


def trim_window_process_tree(
    hwnd: int,
    parent_by_pid: dict[int, int] | None = None,
) -> int:
    """Ask Windows to trim resident pages for the Chrome process tree."""
    if sys.platform != "win32" or not is_window(hwnd):
        return 0
    root = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(root))
    if not root.value:
        return 0
    parents = parent_by_pid if parent_by_pid is not None else snapshot_process_parents()
    pids = descendant_process_ids(int(root.value), parents)
    trimmed = 0
    for pid in pids:
        # PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION
        handle = _kernel32.OpenProcess(0x0100 | 0x0400, False, pid)
        if not handle:
            continue
        try:
            if _psapi.EmptyWorkingSet(handle):
                trimmed += 1
        finally:
            _kernel32.CloseHandle(handle)
    return trimmed


def find_chrome_windows(title: str) -> list[int]:
    if sys.platform != "win32":
        return []
    exact: list[int] = []
    partial: list[int] = []
    wanted = title.strip().casefold()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        if class_buffer.value != "Chrome_WidgetWin_1":
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        text_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        _user32.GetWindowTextW(hwnd, text_buffer, len(text_buffer))
        current = text_buffer.value.strip().casefold()
        if current == wanted:
            exact.append(int(hwnd))
        elif wanted and wanted in current:
            partial.append(int(hwnd))
        return True

    _user32.EnumWindows(collect, 0)
    return exact + partial


def find_chrome_window(title: str) -> int | None:
    matches = find_chrome_windows(title)
    return matches[0] if matches else None


def set_taskbar_group(hwnd: int, app_id: str = TASKBAR_APP_ID) -> bool:
    """Set a shared Windows AppUserModelID so profile windows use one taskbar icon."""
    if not is_window(hwnd):
        return False
    store = ctypes.c_void_p()
    _shell32.SHGetPropertyStoreForWindow.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    result = _shell32.SHGetPropertyStoreForWindow(
        wintypes.HWND(hwnd),
        ctypes.byref(_IID_IPROPERTY_STORE),
        ctypes.byref(store),
    )
    if result < 0 or not store.value:
        return False
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    set_value = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.POINTER(_PROPERTYKEY),
        ctypes.POINTER(_PROPVARIANT),
    )(vtable[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
    value_buffer = ctypes.create_unicode_buffer(app_id)
    value = _PROPVARIANT()
    value.vt = 31  # VT_LPWSTR
    value.pwszVal = ctypes.cast(value_buffer, ctypes.c_wchar_p)
    try:
        result = set_value(
            store,
            ctypes.byref(_PKEY_APP_USER_MODEL_ID),
            ctypes.byref(value),
        )
        if result < 0:
            return False
        return commit(store) >= 0
    finally:
        release(store)


def get_window_rect(hwnd: int) -> WindowRect:
    if not is_window(hwnd):
        raise RuntimeError("Cửa sổ Chrome không còn tồn tại")
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return WindowRect(rect.left, rect.top, rect.right, rect.bottom)


def get_visible_window_rect(hwnd: int) -> WindowRect:
    """Return the visible frame, excluding Windows' invisible resize border."""
    outer = get_window_rect(hwnd)
    if sys.platform != "win32":
        return outer
    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS omits the invisible resize margin which is
    # included by GetWindowRect on Windows 10/11.
    result = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if result != 0:
        return outer
    return WindowRect(rect.left, rect.top, rect.right, rect.bottom)


def get_client_size(hwnd: int) -> tuple[int, int]:
    if not is_window(hwnd):
        raise RuntimeError("Cửa sổ Chrome không còn tồn tại")
    rect = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.right - rect.left, rect.bottom - rect.top


def get_renderer_rect(hwnd: int) -> WindowRect:
    """Return the largest visible Chromium web-content child rectangle."""
    if not is_window(hwnd):
        raise RuntimeError("Cửa sổ Chrome không còn tồn tại")
    candidates: list[WindowRect] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(child: int, _lparam: int) -> bool:
        class_buffer = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(child, class_buffer, len(class_buffer))
        if class_buffer.value != "Chrome_RenderWidgetHostHWND":
            return True
        if not _user32.IsWindowVisible(child):
            return True
        rect = wintypes.RECT()
        if _user32.GetWindowRect(child, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width > 0 and height > 0:
                candidates.append(WindowRect(rect.left, rect.top, rect.right, rect.bottom))
        return True

    _user32.EnumChildWindows(wintypes.HWND(hwnd), collect, 0)
    if candidates:
        return max(candidates, key=lambda rect: rect.width * rect.height)
    outer = get_window_rect(hwnd)
    client_width, client_height = get_client_size(hwnd)
    return WindowRect(
        outer.left,
        outer.top,
        outer.left + client_width,
        outer.top + client_height,
    )


def get_renderer_size(hwnd: int) -> tuple[int, int]:
    rect = get_renderer_rect(hwnd)
    return rect.width, rect.height


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_rgb_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode tightly packed top-down RGB pixels without an image dependency."""
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("Dữ liệu RGB không khớp kích thước ảnh")
    stride = width * 3
    scanlines = b"".join(
        b"\0" + rgb[offset : offset + stride]
        for offset in range(0, len(rgb), stride)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=1))
        + _png_chunk(b"IEND", b"")
    )


def is_region_visible_for_window(hwnd: int, rect: WindowRect) -> bool:
    """Check that the usable interior of a region belongs to its Chrome window.

    WindowFromPoint may report Chrome's renderer child instead of the exact
    top-level HWND.  In app-mode this child can have a different root while
    still belonging to the same isolated Chrome profile process.  Treat that
    process match as belonging to the profile, otherwise a fully visible game
    is falsely marked covered.  We still require a strong interior majority,
    so a real overlay from another process remains blocked.
    """
    if sys.platform != "win32" or not is_window(hwnd) or _user32.IsIconic(hwnd):
        return False
    if rect.width <= 0 or rect.height <= 0:
        return False
    target_pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(target_pid))
    matched = total = 0
    for y_ratio in (0.20, 0.35, 0.50, 0.65, 0.80):
        for x_ratio in (0.20, 0.35, 0.50, 0.65, 0.80):
            total += 1
            point = wintypes.POINT(
                rect.left + int((rect.width - 1) * x_ratio),
                rect.top + int((rect.height - 1) * y_ratio),
            )
            hit = _user32.WindowFromPoint(point)
            if not hit:
                continue
            root = _user32.GetAncestor(hit, 2)  # GA_ROOT
            hit_pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hit, ctypes.byref(hit_pid))
            if int(root or 0) == int(hwnd) or (
                target_pid.value and hit_pid.value == target_pid.value
            ):
                matched += 1
    return matched * 100 >= total * 80


def capture_screen_region_png(rect: WindowRect) -> bytes:
    """Read desktop pixels with GDI; this does not ask Chrome to redraw its surface."""
    if sys.platform != "win32":
        raise RuntimeError("Chụp vùng màn hình chỉ hỗ trợ Windows")
    width, height = rect.width, rect.height
    if width <= 0 or height <= 0:
        raise ValueError("Vùng chụp màn hình không hợp lệ")
    screen_dc = _user32.GetDC(0)
    if not screen_dc:
        raise ctypes.WinError(ctypes.get_last_error())
    memory_dc = _gdi32.CreateCompatibleDC(screen_dc)
    bitmap = old_bitmap = None
    try:
        if not memory_dc:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down pixels
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            screen_dc,
            ctypes.byref(info),
            0,
            ctypes.byref(bits),
            None,
            0,
        )
        if not bitmap or not bits.value:
            raise ctypes.WinError(ctypes.get_last_error())
        old_bitmap = _gdi32.SelectObject(memory_dc, bitmap)
        copied = _gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            rect.left,
            rect.top,
            0x00CC0020 | 0x40000000,  # SRCCOPY | CAPTUREBLT
        )
        if not copied:
            raise ctypes.WinError(ctypes.get_last_error())
        bgra = ctypes.string_at(bits.value, width * height * 4)
        rgb = bytearray(width * height * 3)
        rgb[0::3] = bgra[2::4]
        rgb[1::3] = bgra[1::4]
        rgb[2::3] = bgra[0::4]
        return encode_rgb_png(width, height, bytes(rgb))
    finally:
        if old_bitmap and memory_dc:
            _gdi32.SelectObject(memory_dc, old_bitmap)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if memory_dc:
            _gdi32.DeleteDC(memory_dc)
        _user32.ReleaseDC(0, screen_dc)


def get_work_area() -> WindowRect:
    if sys.platform != "win32":
        return WindowRect(0, 0, 1920, 1080)
    rect = wintypes.RECT()
    if not _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return WindowRect(rect.left, rect.top, rect.right, rect.bottom)


def outer_size_for_client(hwnd: int, client_width: int, client_height: int) -> tuple[int, int]:
    outer = get_window_rect(hwnd)
    current_client_width, current_client_height = get_client_size(hwnd)
    return (
        int(client_width) + max(0, outer.width - current_client_width),
        int(client_height) + max(0, outer.height - current_client_height),
    )


def outer_size_for_renderer(
    hwnd: int,
    renderer_width: int,
    renderer_height: int,
) -> tuple[int, int]:
    outer = get_window_rect(hwnd)
    renderer = get_renderer_rect(hwnd)
    side_frame = max(0, renderer.left - outer.left)
    top_frame = max(0, renderer.top - outer.top)
    return (
        int(renderer_width) + side_frame * 2,
        int(renderer_height) + top_frame + side_frame,
    )


def move_window_client(
    hwnd: int,
    x: int,
    y: int,
    client_width: int,
    client_height: int,
    *,
    topmost: bool,
) -> None:
    outer_width, outer_height = outer_size_for_client(hwnd, client_width, client_height)
    insert_after = -1 if topmost else -2  # HWND_TOPMOST / HWND_NOTOPMOST
    flags = 0x0010 | 0x0040  # SWP_NOACTIVATE | SWP_SHOWWINDOW
    if not _user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(insert_after),
        int(x),
        int(y),
        outer_width,
        outer_height,
        flags,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def move_window_renderer(
    hwnd: int,
    x: int,
    y: int,
    renderer_width: int,
    renderer_height: int,
    *,
    topmost: bool,
) -> None:
    outer_width, outer_height = outer_size_for_renderer(
        hwnd,
        renderer_width,
        renderer_height,
    )
    insert_after = -1 if topmost else -2
    flags = 0x0010 | 0x0040
    if not _user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(insert_after),
        int(x),
        int(y),
        outer_width,
        outer_height,
        flags,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def move_window_position(hwnd: int, x: int, y: int, *, topmost: bool) -> None:
    """Move a window without changing the size chosen by Chrome/Playwright."""
    rect = get_window_rect(hwnd)
    insert_after = -1 if topmost else -2
    flags = 0x0010 | 0x0040
    if not _user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(insert_after),
        int(x),
        int(y),
        rect.width,
        rect.height,
        flags,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def move_window_outer(
    hwnd: int, x: int, y: int, width: int, height: int, *, topmost: bool
) -> None:
    """Move and resize a native window using its full outer frame size."""
    if not is_window(hwnd):
        raise RuntimeError("Cửa sổ Chrome không còn tồn tại")
    insert_after = -1 if topmost else -2
    flags = 0x0010 | 0x0040  # SWP_NOACTIVATE | SWP_SHOWWINDOW
    if not _user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(insert_after),
        int(x),
        int(y),
        max(1, int(width)),
        max(1, int(height)),
        flags,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def set_topmost(hwnd: int, enabled: bool) -> None:
    if not is_window(hwnd):
        raise RuntimeError("Cửa sổ Chrome không còn tồn tại")
    insert_after = -1 if enabled else -2
    flags = 0x0001 | 0x0002 | 0x0010  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    if not _user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(insert_after),
        0,
        0,
        0,
        0,
        flags,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
