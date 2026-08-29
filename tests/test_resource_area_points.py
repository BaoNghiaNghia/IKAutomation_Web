from random import Random

from ik_chrome_auto.resource_area_points import (
    ResourceAreaPointSelector,
    eligible_city_levels,
    eligible_points,
)


def test_resource_level_area_rules_and_pool_sizes() -> None:
    assert eligible_city_levels(6) == (7, 8)
    assert eligible_city_levels(7) == (7, 8, 9, 10)
    assert eligible_city_levels(8) == (8, 9, 10)
    # The supplied list and the ADB selector both contain 55 Lv8 points,
    # despite the prose labelling it as 56. Do not invent a map coordinate.
    assert len(eligible_points(6)) == 71
    assert len(eligible_points(7)) == 123
    assert len(eligible_points(8)) == 107


def test_selector_is_non_repeating_and_bounded_to_three_attempts() -> None:
    selector = ResourceAreaPointSelector(Random(7))
    selections = [
        selector.next(run_id="run", profile_id="account-2", resource="wood", level=7, area_epoch=1)
        for _ in range(4)
    ]
    assert [selection.attempt for selection in selections] == [1, 2, 3, 3]
    assert all(selection.point is not None for selection in selections[:3])
    assert len({selection.point for selection in selections[:3]}) == 3
    assert selections[3].exhausted
