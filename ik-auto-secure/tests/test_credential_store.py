from __future__ import annotations

import pytest

from ik_chrome_auto.credential_store import TARGET_PREFIX, target_for


def test_credential_target_contains_only_safe_account_identifier() -> None:
    assert target_for("farm-01") == TARGET_PREFIX + "farm-01"
    assert target_for("account_2") == TARGET_PREFIX + "account_2"


@pytest.mark.parametrize("account_id", ["", "Farm 01", "../escape", "a" * 65])
def test_credential_target_rejects_unsafe_account_identifier(account_id: str) -> None:
    with pytest.raises(ValueError):
        target_for(account_id)
