"""Opening and closing Chrome profiles must not send Telegram notifications."""

from __future__ import annotations

import ast
import inspect
import textwrap

from ik_chrome_auto.dashboard import Dashboard


def _telegram_calls(method: object) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_notify_telegram"
    ]


def test_profile_open_close_lifecycle_never_sends_telegram() -> None:
    assert _telegram_calls(Dashboard._advance_farm_login) == []
    assert _telegram_calls(Dashboard._finish_farm_stopping) == []


def test_critical_telegram_notifications_still_available() -> None:
    # Profile errors and actual Farm/Monitor alerts must remain enabled.
    assert _telegram_calls(Dashboard._poll) == ["_notify_telegram"]