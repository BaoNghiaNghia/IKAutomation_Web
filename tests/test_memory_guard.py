from __future__ import annotations

import pytest

from ik_chrome_auto.memory_guard import (
    GIB,
    CommitSnapshot,
    guard_new_chrome_launch,
)


def test_new_chrome_launch_blocked_near_commit_exhaustion() -> None:
    critical = CommitSnapshot(96 * GIB, GIB)
    with pytest.raises(RuntimeError, match="Commit Memory"):
        guard_new_chrome_launch(critical)


def test_new_chrome_launch_rejected_at_ninety_percent_used() -> None:
    critical = CommitSnapshot(100 * GIB, 9 * GIB)
    assert critical.critical
    with pytest.raises(RuntimeError, match="Khong mo them Chrome"):
        guard_new_chrome_launch(critical)


def test_new_chrome_launch_safe_with_sufficient_commit() -> None:
    guard_new_chrome_launch(CommitSnapshot(192 * GIB, 75 * GIB))


def test_existing_cdp_reattach_not_subject_to_launch_only_guard() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "ik_chrome_auto" / "browser.py"
    ).read_text(encoding="utf-8")
    launch_start = source.index("def _start_managed(")
    target = source.index("guard_new_chrome_launch()", launch_start)
    existing_endpoint = source.index("if self._managed_cdp_endpoint is not None", launch_start)
    existing_process = source.index("if existing_processes or self.window_handle", launch_start)
    assert existing_endpoint < existing_process < target