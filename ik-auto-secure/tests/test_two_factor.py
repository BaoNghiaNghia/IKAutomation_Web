from __future__ import annotations

from ik_chrome_auto.two_factor import TwoFactorService


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
