"""Deterministic, non-repeating World Map point pools for area fallback.

The coordinates below are game World Map coordinates, never screen pixels.
They are ported verbatim from the ADB ``ResourceAreaLv2PointSelector``.
"""
from __future__ import annotations

from dataclasses import dataclass
from random import Random, SystemRandom


MapPoint = tuple[int, int]


CITY_LEVEL_POINTS: dict[int, tuple[MapPoint, ...]] = {
    7: (
        (650, 954), (644, 926), (642, 899), (658, 877), (672, 865), (682, 881),
        (688, 907), (698, 925), (705, 946), (678, 947), (674, 914), (709, 883),
        (728, 908), (718, 862), (745, 867), (748, 889),
    ),
    8: (
        (380, 837), (374, 852), (374, 872), (391, 887), (402, 904), (423, 911),
        (444, 896), (474, 880), (474, 851), (453, 841), (425, 833), (402, 841),
        (402, 857), (425, 862), (448, 867), (464, 867), (588, 811), (676, 836),
        (568, 853), (558, 872), (568, 889), (580, 904), (589, 923), (605, 939),
        (606, 893), (612, 861), (599, 829), (594, 857), (592, 879), (616, 810),
        (647, 817), (639, 786), (659, 765), (685, 751), (689, 768), (693, 789),
        (705, 812), (718, 834), (690, 834), (670, 812), (677, 778), (715, 762),
        (737, 761), (734, 781), (737, 808), (757, 827), (771, 846), (797, 843),
        (814, 823), (804, 788), (793, 761), (767, 758), (757, 775), (778, 794),
        (783, 816),
    ),
    9: (
        (524, 747), (513, 763), (502, 776), (492, 786), (489, 808), (481, 819),
        (496, 836), (503, 848), (518, 868), (536, 850), (549, 817), (562, 793),
        (575, 777), (543, 776), (530, 789), (520, 809),
    ),
    10: (
        (397, 615), (388, 628), (377, 635), (365, 643), (354, 659), (335, 672),
        (330, 682), (313, 706), (323, 718), (328, 727), (335, 736), (352, 748),
        (370, 741), (388, 716), (408, 693), (419, 671), (392, 681), (365, 686),
        (363, 714), (381, 662), (583, 681), (564, 694), (544, 707), (535, 722),
        (545, 734), (554, 743), (575, 752), (595, 762), (612, 774), (631, 752),
        (654, 731), (637, 710), (605, 703), (577, 701), (591, 716), (615, 733),
    ),
}

RESOURCE_LEVEL_CITY_LEVELS: dict[int, tuple[int, ...]] = {
    6: (7, 8),
    7: (7, 8, 9, 10),
    8: (8, 9, 10),
}


def eligible_city_levels(resource_level: int) -> tuple[int, ...]:
    return RESOURCE_LEVEL_CITY_LEVELS.get(resource_level, ())


def eligible_points(resource_level: int) -> tuple[MapPoint, ...]:
    return tuple(
        point
        for city_level in eligible_city_levels(resource_level)
        for point in CITY_LEVEL_POINTS[city_level]
    )


@dataclass(frozen=True, slots=True)
class AreaPointSelection:
    point: MapPoint | None
    attempt: int
    max_attempts: int
    remaining: int
    city_levels: tuple[int, ...]

    @property
    def exhausted(self) -> bool:
        return self.point is None


class ResourceAreaPointSelector:
    """Creates an independent shuffled bag per run/profile/resource/level/area."""

    max_attempts = 3

    def __init__(self, random: Random | None = None) -> None:
        self._random = random or SystemRandom()
        # A bag belongs to one running Farm session, device, resource and
        # resource level.  In particular it does *not* reset after reaching
        # a new area: resetting there allowed a previously used coordinate to
        # be selected again forever.
        self._bags: dict[tuple[str, str, str, int], list[MapPoint]] = {}

    def next(
        self,
        *,
        run_id: str,
        profile_id: str,
        resource: str,
        level: int,
        area_epoch: int,
    ) -> AreaPointSelection:
        city_levels = eligible_city_levels(level)
        # ``area_epoch`` remains part of the public call for compatibility
        # with existing worker callers and logs.  It must not partition the
        # point pool; each selected point is non-repeating for this run.
        del area_epoch
        key = (run_id, profile_id, resource, level)
        points = eligible_points(level)
        bag = self._bags.get(key)
        if bag is None:
            bag = list(points)
            self._random.shuffle(bag)
            self._bags[key] = bag
        attempt = len(points) - len(bag) + 1
        if not bag or attempt > self.max_attempts:
            return AreaPointSelection(None, min(attempt, self.max_attempts), self.max_attempts, len(bag), city_levels)
        return AreaPointSelection(bag.pop(0), attempt, self.max_attempts, len(bag), city_levels)

    def clear(self, *, run_id: str, profile_id: str, resource: str, level: int, area_epoch: int) -> None:
        del area_epoch
        self._bags.pop((run_id, profile_id, resource, level), None)
