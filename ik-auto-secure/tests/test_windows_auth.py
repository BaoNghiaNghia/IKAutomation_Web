from __future__ import annotations

import asyncio
import sys

import pytest

import ik_chrome_auto.windows_auth as windows_auth


def test_windows_hello_only_runs_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(windows_auth.WindowsAuthenticationError, match="chỉ hỗ trợ Windows"):
        windows_auth.require_windows_hello(action="xem tài khoản")


def test_hello_unavailable_explains_how_to_enable_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    class Availability:
        AVAILABLE = "available"
        NOT_CONFIGURED_FOR_USER = "not-configured"
        DEVICE_NOT_PRESENT = "missing"
        DISABLED_BY_POLICY = "disabled"
        DEVICE_BUSY = "busy"

    class Verifier:
        @staticmethod
        async def check_availability_async() -> str:
            return Availability.NOT_CONFIGURED_FOR_USER

    class Result:
        VERIFIED = "verified"
        CANCELED = "canceled"
        RETRIES_EXHAUSTED = "retries"

    original_import = __import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "winrt.windows.security.credentials.ui":
            return type("Module", (), {
                "UserConsentVerifier": Verifier,
                "UserConsentVerifierAvailability": Availability,
                "UserConsentVerificationResult": Result,
            })
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(windows_auth.WindowsAuthenticationError, match="Sign-in options"):
        asyncio.run(windows_auth._verify_with_windows_hello("xem tài khoản"))
