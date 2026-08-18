from __future__ import annotations

from ik_chrome_auto.reader import REDACTED, redact, redact_text, redact_url


def test_redact_nested_secret_keys() -> None:
    value = {
        "name": "player",
        "access_token": "abc",
        "nested": {"signature": "sig", "level": 10},
    }

    result = redact(value)

    assert result["name"] == "player"
    assert result["access_token"] == REDACTED
    assert result["nested"]["signature"] == REDACTED
    assert result["nested"]["level"] == 10


def test_redact_url_query() -> None:
    result = redact_url("https://example.test/game?server=1&access_token=abc&signature=sig")

    assert "server=1" in result
    assert "abc" not in result
    assert "signature=sig" not in result
    assert "%3Credacted%3E" in result


def test_redact_text_assignment() -> None:
    result = redact_text('access_token="abc" server=1')

    assert "abc" not in result
    assert REDACTED in result
