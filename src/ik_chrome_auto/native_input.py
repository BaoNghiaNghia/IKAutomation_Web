"""Read-only native input capture for the manual multi-profile Sync mode.

This module deliberately contains no cursor or input injection API.  Browser
workers remain the sole owners of follower Playwright/CDP dispatch.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from ik_chrome_auto.windows import WindowRect, get_renderer_rect, is_region_visible_for_window

NativeInputCallback = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class NativeInputMetrics:
    captured_events: int = 0
    pointermove_received: int = 0
    pointermove_coalesced: int = 0
    wheel_received: int = 0
    reliable_events: int = 0


class NativeInputCaptureService:
    """One process-wide, read-only capture coordinator.

    The Win32 message-loop backend is intentionally isolated behind this
    class. ``feed_*`` methods are also the deterministic boundary used by
    tests; they do not inject anything into Windows or Chrome.
    """

    _instance: NativeInputCaptureService | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._callback: NativeInputCallback | None = None
        self._master_hwnd: int | None = None
        self._renderer_rect: WindowRect | None = None
        self._renderer_refresh_at = 0.0
        self._sequence = 0
        self._gesture_active = False
        self._pressed: set[int] = set()
        self._modifiers: set[int] = set()
        self._active = False
        self.metrics = NativeInputMetrics()
        self._thread: threading.Thread | None = None
        self._thread_ready = threading.Event()
        self._thread_id = 0

    @classmethod
    def instance(cls) -> NativeInputCaptureService:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def start(self, master_hwnd: int, callback: NativeInputCallback) -> WindowRect:
        """Activate capture after resolving master renderer geometry.

        Raw Input is available only on Windows. The service remains usable as
        an injectable test seam elsewhere, but production Sync refuses to
        activate without a valid Windows renderer HWND.
        """
        if sys.platform != "win32":
            raise RuntimeError("Native Sync chỉ hỗ trợ Windows")
        rect = get_renderer_rect(master_hwnd)
        if rect.width <= 0 or rect.height <= 0:
            raise RuntimeError("Không xác định được vùng renderer của master Chrome")
        with self._lock:
            self.clear_master()
            self._master_hwnd = int(master_hwnd)
            self._renderer_rect = rect
            self._renderer_refresh_at = time.monotonic() + 0.15
            self._callback = callback
            self._active = True
        self._start_raw_input_thread()
        return rect

    def clear_master(self) -> None:
        with self._lock:
            self._active = False
            self._callback = None
            self._master_hwnd = None
            self._renderer_rect = None
            self._gesture_active = False
            self._pressed.clear()
            self._modifiers.clear()

    def stop(self) -> None:
        self.clear_master()
        if sys.platform == "win32" and self._thread_id:
            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None
        self._thread_id = 0

    def _start_raw_input_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread_ready.clear()
        self._thread = threading.Thread(target=self._raw_input_loop, name="IK-NativeInput", daemon=True)
        self._thread.start()
        if not self._thread_ready.wait(2.0):
            self.clear_master()
            raise RuntimeError("Không thể khởi tạo Raw Input trong 2 giây")

    def _raw_input_loop(self) -> None:
        """Dedicated hidden-window WM_INPUT loop; capture only, never inject."""
        if sys.platform != "win32":
            self._thread_ready.set(); return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()
        WM_INPUT, WM_DESTROY, RID_INPUT, RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0x00FF, 0x0002, 0x10000003, 0, 1
        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]
        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]
        class RAWMOUSE(ctypes.Structure):
            _fields_ = [("usFlags", wintypes.USHORT), ("ulButtons", wintypes.ULONG), ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]
        class RAWKEYBOARD(ctypes.Structure):
            _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]
        class RAWINPUT(ctypes.Structure):
            _fields_ = [("header", RAWINPUTHEADER), ("data", ctypes.c_byte * 48)]
        WNDPROC = ctypes.WINFUNCTYPE(wintypes.LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HCURSOR), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        @WNDPROC
        def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_INPUT:
                size = wintypes.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
                data = ctypes.create_string_buffer(size.value)
                if size.value and user32.GetRawInputData(lparam, RID_INPUT, data, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)):
                    raw = ctypes.cast(data, ctypes.POINTER(RAWINPUT)).contents
                    self._handle_raw_input(raw, RAWMOUSE, RAWKEYBOARD, RIM_TYPEMOUSE, RIM_TYPEKEYBOARD)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0); return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        name = f"IKNativeInput{threading.get_ident()}"
        wc = WNDCLASSW(0, proc, 0, 0, kernel32.GetModuleHandleW(None), None, None, None, None, name)
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, -3, None, wc.hInstance, None)
        devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(1, 2, 0x100, hwnd), RAWINPUTDEVICE(1, 6, 0x100, hwnd))
        user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE))
        self._thread_ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(1, 2, 1, None), RAWINPUTDEVICE(1, 6, 1, None))
        user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)); user32.DestroyWindow(hwnd)

    def _handle_raw_input(self, raw: Any, mouse_type: Any, key_type: Any, mouse_kind: int, key_kind: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        point = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(point))
        if raw.header.dwType == mouse_kind:
            mouse = ctypes.cast(raw.data, ctypes.POINTER(mouse_type)).contents
            flags, wheel = int(mouse.ulButtons & 0xFFFF), ctypes.c_short((int(mouse.ulButtons) >> 16) & 0xFFFF).value
            if flags & 0x0001: self.feed_pointer("pointerdown", point.x, point.y, button=0, buttons=1)
            elif flags & 0x0002: self.feed_pointer("pointerup", point.x, point.y, button=0)
            elif flags & 0x0400: self.feed_wheel(point.x, point.y, 0, wheel)
            else: self.feed_pointer("pointermove", point.x, point.y)
        elif raw.header.dwType == key_kind:
            key = ctypes.cast(raw.data, ctypes.POINTER(key_type)).contents
            foreground = user32.GetForegroundWindow() == self._master_hwnd
            self.feed_key("keyup" if key.Flags & 1 else "keydown", {"key_code": int(key.VKey), "code": "", "key": "", "repeat": False}, master_foreground=bool(foreground))

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "active": self._active,
                "captured_events": self.metrics.captured_events,
                "reliable_events": self.metrics.reliable_events,
                "pointermove_received": self.metrics.pointermove_received,
                "pointermove_coalesced": self.metrics.pointermove_coalesced,
                "wheel_received": self.metrics.wheel_received,
            }

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def _refresh_rect(self) -> WindowRect | None:
        if self._master_hwnd is None:
            return None
        if time.monotonic() >= self._renderer_refresh_at:
            try:
                self._renderer_rect = get_renderer_rect(self._master_hwnd)
            except Exception:
                self.clear_master()
                return None
            self._renderer_refresh_at = time.monotonic() + 0.15
        return self._renderer_rect

    def _accept_point(self, x: int, y: int, *, reliable: bool) -> tuple[float, float] | None:
        rect = self._refresh_rect()
        if rect is None or self._master_hwnd is None:
            return None
        inside = rect.left <= x < rect.right and rect.top <= y < rect.bottom
        if not inside and not (reliable and self._gesture_active):
            return None
        # New gestures also reject windows visibly covered by another app.
        if inside and not self._gesture_active and not is_region_visible_for_window(self._master_hwnd, rect):
            return None
        return (
            max(0.0, min(1.0, (x - rect.left) / max(1, rect.width))),
            max(0.0, min(1.0, (y - rect.top) / max(1, rect.height))),
        )

    def feed_pointer(self, event_type: str, x: int, y: int, *, button: int = 0, buttons: int = 0) -> bool:
        """Normalize a read native pointer sample; never controls the cursor."""
        with self._lock:
            if not self._active:
                return False
            reliable = event_type in {"pointerdown", "pointerup"}
            point = self._accept_point(int(x), int(y), reliable=reliable)
            if point is None:
                return False
            if event_type == "pointerdown":
                self._gesture_active = True
            event = {"type": event_type, "sequence": self._next(), "viewport": {"ratio_x": point[0], "ratio_y": point[1]}, "pointer": {"button": int(button), "buttons": int(buttons)}}
            callback = self._callback
            self.metrics.captured_events += 1
            if reliable:
                self.metrics.reliable_events += 1
            else:
                self.metrics.pointermove_received += 1
            if event_type == "pointerup":
                self._gesture_active = False
        if callback:
            callback(event)
        return True

    def feed_wheel(self, x: int, y: int, delta_x: float, delta_y: float) -> bool:
        with self._lock:
            if not self._active:
                return False
            point = self._accept_point(x, y, reliable=False)
            if point is None:
                return False
            event = {"type": "wheel", "sequence": self._next(), "viewport": {"ratio_x": point[0], "ratio_y": point[1]}, "wheel": {"delta_x": float(delta_x), "delta_y": float(delta_y)}}
            callback = self._callback
            self.metrics.captured_events += 1
            self.metrics.wheel_received += 1
        if callback:
            callback(event)
        return True

    def feed_key(self, event_type: str, keyboard: dict[str, Any], *, master_foreground: bool) -> bool:
        with self._lock:
            if not self._active or not master_foreground:
                return False
            event = {"type": event_type, "sequence": self._next(), "keyboard": dict(keyboard)}
            callback = self._callback
            self.metrics.captured_events += 1
            self.metrics.reliable_events += 1
        if callback:
            callback(event)
        return True
