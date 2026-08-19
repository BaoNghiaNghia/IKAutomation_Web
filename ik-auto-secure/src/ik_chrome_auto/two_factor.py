"""Local TOTP security for sensitive account-management operations.

The TOTP secret and recovery-code hashes never enter config.json.  They are
kept in the current Windows user's Credential Manager alongside game secrets.
"""
from __future__ import annotations

import base64
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


@dataclass(frozen=True, slots=True)
class TwoFactorEnrollment:
    secret: str
    provisioning_uri: str
    recovery_codes: tuple[str, ...]


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

    def confirm_enrollment(self, enrollment: TwoFactorEnrollment, code: str) -> bool:
        if not self.verify_code(enrollment.secret, code):
            return False
        payload = self._recovery_payload(enrollment.secret, enrollment.recovery_codes)
        self.store.save(AccountCredential(_TOTP_ACCOUNT_ID, "Google Authenticator", enrollment.secret))
        self.store.save(AccountCredential(_RECOVERY_ACCOUNT_ID, "One-time recovery codes", json.dumps(payload)))
        return True

    def verify_current_code(self, code: str) -> bool:
        credential = self.store.load(_TOTP_ACCOUNT_ID)
        return credential is not None and self.verify_code(credential.password, code)

    def consume_recovery_code(self, code: str) -> bool:
        totp = self.store.load(_TOTP_ACCOUNT_ID)
        recovery = self.store.load(_RECOVERY_ACCOUNT_ID)
        if totp is None or recovery is None:
            return False
        try:
            entries = json.loads(recovery.password)
            digest = self._recovery_digest(totp.password, code)
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
    def verify_code(secret: str, code: str, now: float | None = None) -> bool:
        candidate = "".join(character for character in code if character.isdigit())
        if len(candidate) != 6:
            return False
        timestamp = int(time.time() if now is None else now)
        return any(
            hmac.compare_digest(TwoFactorService._totp(secret, (timestamp // _STEP_SECONDS) + offset), candidate)
            for offset in (-1, 0, 1)
        )

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
