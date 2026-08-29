from __future__ import annotations

import pytest

from ik_chrome_auto.credential_store import (
    TARGET_PREFIX,
    escape_account_export_field,
    parse_account_export_line,
    target_for,
)


def test_credential_target_contains_only_safe_account_identifier() -> None:
    assert target_for("farm-01") == TARGET_PREFIX + "farm-01"
    assert target_for("account_2") == TARGET_PREFIX + "account_2"


@pytest.mark.parametrize("account_id", ["", "Farm 01", "../escape", "a" * 65])
def test_credential_target_rejects_unsafe_account_identifier(account_id: str) -> None:
    with pytest.raises(ValueError):
        target_for(account_id)


def test_account_export_line_round_trips_delimiters_and_newlines() -> None:
    values = ("Tài khoản 01", "player@example.com", "p|ass\\word\nnext")

    line = "|".join(escape_account_export_field(value) for value in values)

    assert parse_account_export_line(line) == values


@pytest.mark.parametrize(
    "line",
    (
        "missing|one-field",
        "|player@example.com|password",
        "Tài khoản|player name|password",
        "Tài khoản|player@example.com|",
    ),
)
def test_account_export_line_rejects_invalid_rows(line: str) -> None:
    assert parse_account_export_line(line) is None
