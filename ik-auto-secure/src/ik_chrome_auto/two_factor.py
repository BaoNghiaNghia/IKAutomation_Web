"""Local TOTP security for sensitive account-management operations.

The TOTP secret and recovery-code hashes never enter config.json.  They are
kept in the current Windows user's Credential Manager alongside game secrets.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote

from ik_chrome_auto.credential_store import AccountCredential, WindowsCredentialStore

_TOTP_ACCOUNT_ID = "security-totp"
_RECOVERY_ACCOUNT_ID = "security-recovery"
_ISSUER = "IK Auto"
_STEP_SECONDS = 30
_RECOVERY_COUNT = 10
_ENROLLMENT_WINDOW_STEPS = 10
_TOTP_RECORD_VERSION = 2


@dataclass(frozen=True, slots=True)
class TwoFactorEnrollment:
    secret: str
    provisioning_uri: str
    recovery_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TotpRecord:
    secret: str
    time_offset_steps: int = 0


class TwoFactorService:
    """Creates and verifies authenticator codes for the current Windows user."""

    def __init__(self, store: WindowsCredentialStore | None = None) -> None:
        self.store = store or WindowsCredentialStore()

    def is_configured(self) -> bool:
        return self.store.exists(_TOTP_ACCOUNT_ID)

    def begin_enrollment(self) -> TwoFactorEnrollment:
        secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
        codes = tuple(self._new_recovery_code() for _ in range(_RECOVERY_COUNT))
        label = quote(f"{_ISSUER}:Local account", safe="")
        uri = f"otpauth://totp/{label}?secret={secret}&issuer={quote(_ISSUER)}&period={_STEP_SECONDS}&digits=6"
        return TwoFactorEnrollment(secret, uri, codes)

    def confirm_enrollment(self, enrollment: TwoFactorEnrollment, code: str, now: float | None = None) -> bool:
        time_offset_steps = self._matching_offset(
            enrollment.secret,
            code,
            now=now,
            max_offset_steps=_ENROLLMENT_WINDOW_STEPS,
        )
        if time_offset_steps is None:
            return False
        payload = self._recovery_payload(enrollment.secret, enrollment.recovery_codes)
        totp_payload = json.dumps(
            {
                "version": _TOTP_RECORD_VERSION,
                "secret": enrollment.secret,
                "time_offset_steps": time_offset_steps,
            },
            separators=(",", ":"),
        )
        self.store.save(AccountCredential(_TOTP_ACCOUNT_ID, "Google Authenticator", totp_payload))
        self.store.save(AccountCredential(_RECOVERY_ACCOUNT_ID, "One-time recovery codes", json.dumps(payload)))
        return True

    def verify_current_code(self, code: str, now: float | None = None) -> bool:
        credential = self.store.load(_TOTP_ACCOUNT_ID)
        if credential is None:
            return False
        record = self._decode_totp_record(credential.password)
        return record is not None and self.verify_code(
            record.secret,
            code,
            now=now,
            offset_steps=record.time_offset_steps,
        )

    def consume_recovery_code(self, code: str) -> bool:
        totp = self.store.load(_TOTP_ACCOUNT_ID)
        recovery = self.store.load(_RECOVERY_ACCOUNT_ID)
        if totp is None or recovery is None:
            return False
        try:
            entries = json.loads(recovery.password)
            record = self._decode_totp_record(totp.password)
            if record is None:
                return False
            digest = self._recovery_digest(record.secret, code)
            hashes = list(entries["hashes"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        for index, value in enumerate(hashes):
            if hmac.compare_digest(digest, str(value)):
                del hashes[index]
                self.store.save(AccountCredential(_RECOVERY_ACCOUNT_ID, "One-time recovery codes", json.dumps({"hashes": hashes})))
                return True
        return False

    @staticmethod
    def verify_code(
        secret: str,
        code: str,
        now: float | None = None,
        *,
        offset_steps: int = 0,
        window_steps: int = 1,
    ) -> bool:
        return TwoFactorService._matching_offset(
            secret,
            code,
            now=now,
            center_offset_steps=offset_steps,
            max_offset_steps=window_steps,
        ) is not None

    @staticmethod
    def verify_enrollment_code(secret: str, code: str, now: float | None = None) -> bool:
        """Allow enrollment to calibrate a modest phone/Windows clock skew."""
        return TwoFactorService._matching_offset(
            secret,
            code,
            now=now,
            max_offset_steps=_ENROLLMENT_WINDOW_STEPS,
        ) is not None

    @staticmethod
    def _matching_offset(
        secret: str,
        code: str,
        now: float | None = None,
        *,
        center_offset_steps: int = 0,
        max_offset_steps: int = 1,
    ) -> int | None:
        candidate = "".join(character for character in code if character.isdigit())
        if len(candidate) != 6:
            return None
        timestamp = int(time.time() if now is None else now)
        deltas = [0]
        for distance in range(1, max(0, max_offset_steps) + 1):
            deltas.extend((-distance, distance))
        try:
            for delta in deltas:
                offset = center_offset_steps + delta
                expected = TwoFactorService._totp(secret, (timestamp // _STEP_SECONDS) + offset)
                if hmac.compare_digest(expected, candidate):
                    return offset
        except (ValueError, TypeError, binascii.Error):
            return None
        return None

    @staticmethod
    def _decode_totp_record(value: str) -> _TotpRecord | None:
        """Read v2 JSON records and legacy records that stored the raw secret."""
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            try:
                secret = str(payload["secret"]).strip()
                offset = int(payload.get("time_offset_steps", 0))
            except (KeyError, TypeError, ValueError):
                return None
            return _TotpRecord(secret, offset) if secret else None
        secret = str(value).strip()
        return _TotpRecord(secret) if secret else None

    @staticmethod
    def _totp(secret: str, counter: int) -> str:
        padded = secret.upper() + "=" * (-len(secret) % 8)
        key = base64.b32decode(padded, casefold=True)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        number = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{number:06d}"

    @staticmethod
    def _new_recovery_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"

    @classmethod
    def _recovery_payload(cls, secret: str, codes: tuple[str, ...]) -> dict[str, list[str]]:
        return {"hashes": [cls._recovery_digest(secret, code) for code in codes]}

    @staticmethod
    def _recovery_digest(secret: str, code: str) -> str:
        normalized = code.replace("-", "").replace(" ", "").upper()
        return hmac.new(secret.encode("ascii"), normalized.encode("ascii"), hashlib.sha256).hexdigest()
