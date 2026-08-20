from __future__ import annotations

from ik_chrome_auto.credential_store import AccountCredential
from ik_chrome_auto.two_factor import TwoFactorEnrollment, TwoFactorService


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, AccountCredential] = {}

    def exists(self, account_id: str) -> bool:
        return account_id in self.values

    def load(self, account_id: str) -> AccountCredential | None:
        return self.values.get(account_id)

    def save(self, credential: AccountCredential) -> None:
        self.values[credential.account_id] = credential


def test_totp_accepts_current_code_and_one_step_clock_drift() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    timestamp = 1_700_000_000
    code = TwoFactorService._totp(secret, timestamp // 30)

    assert TwoFactorService.verify_code(secret, code, now=timestamp)
    assert TwoFactorService.verify_code(secret, code, now=timestamp + 30)
    assert not TwoFactorService.verify_code(secret, "000000", now=timestamp)


def test_recovery_code_format_and_hashes_are_not_plaintext() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    code = TwoFactorService._new_recovery_code()
    payload = TwoFactorService._recovery_payload(secret, (code,))

    assert code.count("-") == 2
    assert code not in payload["hashes"]
    assert len(payload["hashes"][0]) == 64


def test_enrollment_calibrates_clock_skew_and_reuses_it_for_verification() -> None:
    timestamp = 1_700_000_000
    secret = "JBSWY3DPEHPK3PXP"
    phone_offset_steps = 4
    code = TwoFactorService._totp(secret, timestamp // 30 + phone_offset_steps)
    enrollment = TwoFactorEnrollment(secret, "otpauth://test", ("ABCD-EFGH-JKLM",))
    store = _MemoryStore()
    service = TwoFactorService(store)  # type: ignore[arg-type]

    assert service.confirm_enrollment(enrollment, code, now=timestamp)
    assert service.verify_current_code(code, now=timestamp)


def test_legacy_raw_secret_remains_supported() -> None:
    timestamp = 1_700_000_000
    secret = "JBSWY3DPEHPK3PXP"
    code = TwoFactorService._totp(secret, timestamp // 30)
    store = _MemoryStore()
    store.save(AccountCredential("security-totp", "Google Authenticator", secret))

    assert TwoFactorService(store).verify_current_code(code, now=timestamp)  # type: ignore[arg-type]
