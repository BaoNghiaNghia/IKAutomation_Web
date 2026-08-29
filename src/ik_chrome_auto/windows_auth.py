"""Windows Hello confirmation for sensitive account operations."""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time
from ctypes import wintypes


class WindowsAuthenticationError(RuntimeError):
    pass


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG), ("top", wintypes.LONG),
        ("right", wintypes.LONG), ("bottom", wintypes.LONG),
    ]


def _center_windows_security_prompt(stop_event: threading.Event) -> None:
    """Center the OS-owned Hello dialog and keep it above normal app windows."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def find_prompt(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value.casefold() not in {"windows security", "bảo mật windows"}:
            return True
        bounds = _RECT()
        work_area = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(bounds)):
            return True
        user32.SystemParametersInfoW(48, 0, ctypes.byref(work_area), 0)  # SPI_GETWORKAREA
        width, height = bounds.right - bounds.left, bounds.bottom - bounds.top
        x = work_area.left + max(0, (work_area.right - work_area.left - width) // 2)
        y = work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2)
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, 0, 0, 0x0001 | 0x0040)
        user32.SetForegroundWindow(hwnd)
        return False

    callback = enum_callback(find_prompt)
    deadline = time.monotonic() + 5.0
    while not stop_event.is_set() and time.monotonic() < deadline:
        user32.EnumWindows(callback, 0)
        time.sleep(0.04)


async def _verify_with_windows_hello(action: str) -> bool:
    try:
        from winrt.windows.security.credentials.ui import (
            UserConsentVerificationResult,
            UserConsentVerifier,
            UserConsentVerifierAvailability,
        )
    except ImportError as error:
        raise WindowsAuthenticationError(
            "Thiếu thành phần Windows Hello. Hãy chạy IKAutomation_dev.cmd để tự cài đặt."
        ) from error

    availability = await UserConsentVerifier.check_availability_async()
    if availability != UserConsentVerifierAvailability.AVAILABLE:
        messages = {
            UserConsentVerifierAvailability.DEVICE_NOT_PRESENT: "Thiết bị này không hỗ trợ Windows Hello.",
            UserConsentVerifierAvailability.NOT_CONFIGURED_FOR_USER: "Hãy cài đặt Windows Hello PIN trong Settings > Accounts > Sign-in options.",
            UserConsentVerifierAvailability.DISABLED_BY_POLICY: "Windows Hello đang bị chính sách hệ thống vô hiệu hóa.",
            UserConsentVerifierAvailability.DEVICE_BUSY: "Windows Hello đang bận, hãy thử lại sau.",
        }
        raise WindowsAuthenticationError(messages.get(availability, "Windows Hello không sẵn sàng."))

    positioning_done = threading.Event()
    positioner = threading.Thread(
        target=_center_windows_security_prompt,
        args=(positioning_done,),
        daemon=True,
    )
    positioner.start()
    try:
        result = await UserConsentVerifier.request_verification_async(
            f"Xác nhận bằng Windows Hello để {action}."
        )
    finally:
        positioning_done.set()
        positioner.join(timeout=0.2)
    if result == UserConsentVerificationResult.VERIFIED:
        return True
    if result == UserConsentVerificationResult.CANCELED:
        return False
    if result == UserConsentVerificationResult.RETRIES_EXHAUSTED:
        raise WindowsAuthenticationError("Đã vượt quá số lần thử Windows Hello.")
    raise WindowsAuthenticationError("Windows Hello không thể xác thực thao tác này.")


def require_windows_hello(*, action: str) -> bool:
    """Show the OS-owned Windows Hello prompt; no secret enters this app."""
    if sys.platform != "win32":
        raise WindowsAuthenticationError("Windows Hello chỉ hỗ trợ Windows")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_verify_with_windows_hello(action))
    raise WindowsAuthenticationError("Không thể mở Windows Hello khi vòng lặp xác thực đang chạy")
