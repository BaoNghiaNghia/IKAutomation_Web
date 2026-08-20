from __future__ import annotations

from types import SimpleNamespace

from ik_chrome_auto.runner import ProfileWorker


def evidence(bounds: tuple[int, int, int, int]):
    return SimpleNamespace(actionable=True, bounds=bounds)


def test_team_one_row_is_inferred_from_numbered_rows_below_it() -> None:
    badges = {
        2: evidence((4, 100, 24, 24)),
        3: evidence((4, 191, 24, 24)),
        4: evidence((4, 277, 24, 24)),
    }

    row = ProfileWorker._team_row_for_selection(1, badges, (835, 432))

    assert row == (0, 1, 144, 96)


def test_team_one_selected_border_is_verified_against_inferred_row() -> None:
    badges = {
        2: evidence((4, 100, 24, 24)),
        3: evidence((4, 191, 24, 24)),
    }
    row = ProfileWorker._team_row_for_selection(1, badges, (835, 432))
    selected_border = evidence((1, 10, 7, 67))

    assert ProfileWorker._is_expected_team_selected(None, selected_border, row) is True


def test_numbered_team_still_uses_its_own_badge() -> None:
    badges = {3: evidence((4, 191, 24, 24))}

    row = ProfileWorker._team_row_for_selection(3, badges, (835, 432))

    assert row == (0, 183, 144, 96)
