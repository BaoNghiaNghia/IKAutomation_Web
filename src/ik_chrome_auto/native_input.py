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

if sys.platform == "win32":
    ULONG_PTR = ctypes.c_size_t
    LRESULT = ctypes.c_ssize_t
    HRAWINPUT = wintypes.HANDLE
    WM_INPUT, WM_DESTROY, WM_QUIT = 0x00FF, 0x0002, 0x0012
    RID_INPUT, RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0x10000003, 0, 1
    RIDEV_INPUTSINK, RIDEV_REMOVE = 0x00000100, 0x00000001
    RI_KEY_BREAK, RI_KEY_E0, RI_KEY_E1 = 0x0001, 0x0002, 0x0004
    RI_MOUSE_LEFT_BUTTON_DOWN, RI_MOUSE_LEFT_BUTTON_UP = 0x0001, 0x0002
    RI_MOUSE_RIGHT_BUTTON_DOWN, RI_MOUSE_RIGHT_BUTTON_UP = 0x0004, 0x0008
    RI_MOUSE_MIDDLE_BUTTON_DOWN, RI_MOUSE_MIDDLE_BUTTON_UP = 0x0010, 0x0020
    RI_MOUSE_WHEEL, RI_MOUSE_HWHEEL = 0x0400, 0x0800

    class _RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

    class _RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE), ("wParam", ULONG_PTR)]

    class _RAWMOUSEBUTTONS(ctypes.Structure):
        _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]

    class _RAWMOUSEBUTTONUNION(ctypes.Union):
        _anonymous_ = ("buttons",)
        _fields_ = [("ulButtons", wintypes.ULONG), ("buttons", _RAWMOUSEBUTTONS)]

    class _RAWMOUSE(ctypes.Structure):
        _anonymous_ = ("button_union",)
        _fields_ = [("usFlags", wintypes.USHORT), ("button_union", _RAWMOUSEBUTTONUNION), ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]

    class _RAWKEYBOARD(ctypes.Structure):
        _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]

    class _RAWHID(ctypes.Structure):
        _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD), ("bRawData", ctypes.c_byte * 1)]

    class _RAWINPUTUNION(ctypes.Union):
        _fields_ = [("mouse", _RAWMOUSE), ("keyboard", _RAWKEYBOARD), ("hid", _RAWHID)]

    class _RAWINPUT(ctypes.Structure):
        _anonymous_ = ("data",)
        _fields_ = [("header", _RAWINPUTHEADER), ("data", _RAWINPUTUNION)]

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    def _configure_win32(user32: Any, kernel32: Any) -> None:
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]; kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.argtypes = []; kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]; user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]; user32.UnregisterClassW.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]; user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]; user32.DestroyWindow.restype = wintypes.BOOL
        user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(_RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]; user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.GetRawInputData.argtypes = [HRAWINPUT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]; user32.GetRawInputData.restype = wintypes.UINT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]; user32.DefWindowProcW.restype = LRESULT
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]; user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]; user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]; user32.DispatchMessageW.restype = LRESULT
        user32.PostQuitMessage.argtypes = [ctypes.c_int]; user32.PostQuitMessage.restype = None
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]; user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]; user32.GetCursorPos.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []; user32.GetForegroundWindow.restype = wintypes.HWND


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
        self._startup_error: Exception | None = None
        self._native_hwnd: int | None = None
        self._registered = False
        self._diagnostics: list[dict[str, object]] = []

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
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            _configure_win32(user32, ctypes.WinDLL("kernel32", use_last_error=True))
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None
        self._thread_id = 0
        self._native_hwnd = None
        self._registered = False

    def _start_raw_input_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread_ready.clear()
        self._startup_error = None
        self._native_hwnd = None
        self._registered = False
        self._thread = threading.Thread(target=self._raw_input_loop, name="IK-NativeInput", daemon=True)
        self._thread.start()
        if not self._thread_ready.wait(2.0):
            self.clear_master()
            raise RuntimeError("Không thể khởi tạo Raw Input trong 2 giây")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise RuntimeError(str(error)) from error
        if not self._registered or not self._native_hwnd or not self._thread.is_alive():
            self.stop()
            raise RuntimeError("Raw Input khởi tạo không hoàn tất")

    def _raw_input_loop(self) -> None:
        """Dedicated hidden-window WM_INPUT loop; capture only, never inject."""
        if sys.platform != "win32":
            self._thread_ready.set(); return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_win32(user32, kernel32)
        self._thread_id = kernel32.GetCurrentThreadId()
        @WNDPROC
        def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_INPUT:
                size = wintypes.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(_RAWINPUTHEADER))
                data = ctypes.create_string_buffer(size.value)
                read = user32.GetRawInputData(lparam, RID_INPUT, data, ctypes.byref(size), ctypes.sizeof(_RAWINPUTHEADER))
                if size.value and read != 0xFFFFFFFF:
                    raw = ctypes.cast(data, ctypes.POINTER(_RAWINPUT)).contents
                    self._handle_raw_input(raw)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0); return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        name = f"IKNativeInput{threading.get_ident()}"
        instance = kernel32.GetModuleHandleW(None)
        wc = _WNDCLASSW(0, proc, 0, 0, instance, None, None, None, None, name)
        try:
            atom = user32.RegisterClassW(ctypes.byref(wc))
            error = ctypes.get_last_error()
            if not atom and error != 1410:
                raise RuntimeError(f"RegisterClassW failed ({error}): {ctypes.FormatError(error)}")
            self._diagnostics.append({"event": "native_input_thread_started"})
            hwnd = user32.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, wintypes.HWND(-3), None, instance, None)
            if not hwnd:
                error = ctypes.get_last_error(); raise RuntimeError(f"CreateWindowExW failed ({error}): {ctypes.FormatError(error)}")
            self._native_hwnd = int(hwnd)
            self._diagnostics.append({"event": "native_input_window_created", "native_hwnd": int(hwnd)})
            devices = (_RAWINPUTDEVICE * 2)(_RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd), _RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK, hwnd))
            if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(_RAWINPUTDEVICE)):
                error = ctypes.get_last_error(); raise RuntimeError(f"RegisterRawInputDevices failed ({error}): {ctypes.FormatError(error)}")
            self._registered = True
            self._diagnostics.extend(({"event": "native_input_mouse_registered"}, {"event": "native_input_keyboard_registered"}, {"event": "native_input_ready"}))
        except Exception as error:
            self._startup_error = error
            self._diagnostics.append({"event": "native_input_init_failed", "message": str(error)})
            self._thread_ready.set()
            return
        self._thread_ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
        devices = (_RAWINPUTDEVICE * 2)(_RAWINPUTDEVICE(1, 2, RIDEV_REMOVE, None), _RAWINPUTDEVICE(1, 6, RIDEV_REMOVE, None))
        user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(_RAWINPUTDEVICE)); user32.DestroyWindow(hwnd); user32.UnregisterClassW(name, instance)

    def _handle_raw_input(self, raw: Any) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        _configure_win32(user32, ctypes.WinDLL("kernel32", use_last_error=True))
        point = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(point))
        if raw.header.dwType == RIM_TYPEMOUSE:
            mouse, flags = raw.mouse, int(raw.mouse.usButtonFlags)
            button_map = ((RI_MOUSE_LEFT_BUTTON_DOWN, "pointerdown", 0, 1), (RI_MOUSE_LEFT_BUTTON_UP, "pointerup", 0, 0), (RI_MOUSE_RIGHT_BUTTON_DOWN, "pointerdown", 2, 2), (RI_MOUSE_RIGHT_BUTTON_UP, "pointerup", 2, 0), (RI_MOUSE_MIDDLE_BUTTON_DOWN, "pointerdown", 1, 4), (RI_MOUSE_MIDDLE_BUTTON_UP, "pointerup", 1, 0))
            for mask, kind, button, buttons in button_map:
                if flags & mask:
                    self.feed_pointer(kind, point.x, point.y, button=button, buttons=buttons)
            wheel = ctypes.c_short(int(mouse.usButtonData)).value
            if flags & RI_MOUSE_WHEEL: self.feed_wheel(point.x, point.y, 0, wheel)
            if flags & RI_MOUSE_HWHEEL: self.feed_wheel(point.x, point.y, wheel, 0)
            if int(mouse.lLastX) or int(mouse.lLastY): self.feed_pointer("pointermove", point.x, point.y)
        elif raw.header.dwType == RIM_TYPEKEYBOARD:
            key = raw.keyboard
            flags, vk = int(key.Flags), int(key.VKey)
            is_up = bool(flags & RI_KEY_BREAK)
            repeat = not is_up and vk in self._pressed
            if is_up: self._pressed.discard(vk)
            else: self._pressed.add(vk)
            modifier = {16: "shift", 17: "ctrl", 18: "alt"}.get(vk)
            if modifier:
                (self._modifiers.discard if is_up else self._modifiers.add)(vk)
            foreground = self._is_master_foreground(user32)
            self.feed_key("keyup" if is_up else "keydown", self._keyboard_payload(vk, int(key.MakeCode), flags, repeat), master_foreground=foreground)

    def _is_master_foreground(self, user32: Any) -> bool:
        foreground = int(user32.GetForegroundWindow() or 0)
        return foreground == int(self._master_hwnd or 0)

    def _keyboard_payload(self, vk: int, scan_code: int, flags: int, repeat: bool) -> dict[str, Any]:
        special = {13: ("Enter", "Enter"), 9: ("Tab", "Tab"), 27: ("Escape", "Escape"), 32: (" ", "Space"), 8: ("Backspace", "Backspace"), 46: ("Delete", "Delete"), 37: ("ArrowLeft", "ArrowLeft"), 38: ("ArrowUp", "ArrowUp"), 39: ("ArrowRight", "ArrowRight"), 40: ("ArrowDown", "ArrowDown"), 16: ("Shift", "ShiftLeft"), 17: ("Control", "ControlLeft"), 18: ("Alt", "AltLeft")}
        if 65 <= vk <= 90: key, code = chr(vk).lower(), f"Key{chr(vk)}"
        elif 48 <= vk <= 57: key, code = chr(vk), f"Digit{chr(vk)}"
        elif 112 <= vk <= 123: key, code = f"F{vk - 111}", f"F{vk - 111}"
        else: key, code = special.get(vk, ("", ""))
        return {"key": key, "code": code, "key_code": vk, "scan_code": scan_code, "extended": bool(flags & (RI_KEY_E0 | RI_KEY_E1)), "repeat": repeat, "shift": 16 in self._modifiers, "ctrl": 17 in self._modifiers, "alt": 18 in self._modifiers}

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "active": self._active,
                "captured_events": self.metrics.captured_events,
                "reliable_events": self.metrics.reliable_events,
                "pointermove_received": self.metrics.pointermove_received,
                "pointermove_coalesced": self.metrics.pointermove_coalesced,
                "wheel_received": self.metrics.wheel_received,
                "native_hwnd": self._native_hwnd or 0,
                "registered": self._registered,
            }

    def diagnostics(self) -> tuple[dict[str, object], ...]:
        return tuple(self._diagnostics)

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
