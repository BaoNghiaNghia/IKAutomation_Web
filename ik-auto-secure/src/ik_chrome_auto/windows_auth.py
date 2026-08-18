"""Native Windows-password confirmation for sensitive account operations."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


ERROR_CANCELLED = 1223
CREDUI_FLAGS_DO_NOT_PERSIST = 0x00000002
CREDUI_FLAGS_ALWAYS_SHOW_UI = 0x00000080
CREDUI_FLAGS_GENERIC_CREDENTIALS = 0x00040000
LOGON32_LOGON_INTERACTIVE = 2
LOGON32_PROVIDER_DEFAULT = 0


class WindowsAuthenticationError(RuntimeError):
    pass


class _CREDUI_INFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hwndParent", wintypes.HWND),
        ("pszMessageText", wintypes.LPCWSTR),
        ("pszCaptionText", wintypes.LPCWSTR),
        ("hbmBanner", wintypes.HANDLE),
    ]


def _current_username() -> str:
    size = wintypes.DWORD(257)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
        raise WindowsAuthenticationError("Không lấy được tài khoản Windows hiện tại")
    return buffer.value


def _split_account(username: str) -> tuple[str, str | None]:
    if "\\" in username:
        domain, name = username.split("\\", 1)
        return name, domain
    return username, None


def require_windows_password(*, parent_window: int = 0, action: str) -> bool:
    """Prompt natively and verify the current Windows user's password.

    Returns False when the user cancels. The password buffer is cleared before
    returning and is never logged or persisted by the application.
    """
    if sys.platform != "win32":
        raise WindowsAuthenticationError("Xác thực mật khẩu Windows chỉ hỗ trợ Windows")
    credui = ctypes.WinDLL("Credui.dll", use_last_error=True)
    prompt = credui.CredUIPromptForCredentialsW
    prompt.argtypes = [
        ctypes.POINTER(_CREDUI_INFOW), wintypes.LPCWSTR, ctypes.c_void_p,
        wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPWSTR,
        wintypes.DWORD, ctypes.POINTER(wintypes.BOOL), wintypes.DWORD,
    ]
    prompt.restype = wintypes.DWORD
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    logon_user = advapi32.LogonUserW
    logon_user.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    logon_user.restype = wintypes.BOOL
    current = _current_username()
    username = ctypes.create_unicode_buffer(current, 513)
    password = ctypes.create_unicode_buffer(256)
    save = wintypes.BOOL(False)
    info = _CREDUI_INFOW(
        ctypes.sizeof(_CREDUI_INFOW),
        wintypes.HWND(parent_window),
        f"Nhập mật khẩu đăng nhập Windows để {action}.",
        "IK Auto – Xác thực Windows",
        None,
    )
    try:
        result = prompt(
            ctypes.byref(info),
            "IKAutoSecure/WindowsPasswordConfirmation",
            None,
            0,
            username,
            len(username),
            password,
            len(password),
            ctypes.byref(save),
            CREDUI_FLAGS_DO_NOT_PERSIST
            | CREDUI_FLAGS_ALWAYS_SHOW_UI
            | CREDUI_FLAGS_GENERIC_CREDENTIALS,
        )
        if result == ERROR_CANCELLED:
            return False
        if result != 0:
            raise WindowsAuthenticationError(f"Không mở được hộp thoại xác thực Windows (mã {result})")
        entered_name, domain = _split_account(username.value)
        if entered_name.lower() != current.lower():
            raise WindowsAuthenticationError("Hãy dùng đúng tài khoản Windows hiện đang đăng nhập")
        token = wintypes.HANDLE()
        if not logon_user(
            entered_name,
            domain,
            password,
            LOGON32_LOGON_INTERACTIVE,
            LOGON32_PROVIDER_DEFAULT,
            ctypes.byref(token),
        ):
            raise WindowsAuthenticationError("Mật khẩu Windows không đúng")
        ctypes.windll.kernel32.CloseHandle(token)
        return True
    finally:
        ctypes.memset(password, 0, ctypes.sizeof(password))
