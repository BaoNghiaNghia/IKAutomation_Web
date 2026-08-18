"""Windows Hello confirmation for sensitive account operations."""

from __future__ import annotations

import asyncio
import sys


class WindowsAuthenticationError(RuntimeError):
    pass


async def _verify_with_windows_hello(action: str) -> bool:
    try:
        from winrt.windows.security.credentials.ui import (
            UserConsentVerificationResult,
            UserConsentVerifier,
            UserConsentVerifierAvailability,
        )
    except ImportError as error:
        raise WindowsAuthenticationError(
            "Thiếu thành phần Windows Hello. Hãy chạy run.cmd để tự cài đặt."
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

    result = await UserConsentVerifier.request_verification_async(
        f"Xác nhận bằng Windows Hello để {action}."
    )
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
