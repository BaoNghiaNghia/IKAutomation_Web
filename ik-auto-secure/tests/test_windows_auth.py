from __future__ import annotations

from ik_chrome_auto.windows_auth import _split_account


def test_split_windows_account_supports_domain_and_upn_names() -> None:
    assert _split_account(r"WORKSTATION\bao") == ("bao", "WORKSTATION")
    assert _split_account("bao@example.com") == ("bao@example.com", None)
