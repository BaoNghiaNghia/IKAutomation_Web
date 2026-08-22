from pathlib import Path

import cv2
import numpy as np

from ik_chrome_auto.mail_monitor import BrowserMailMonitor
from ik_chrome_auto.runner import (
    CLOSE_MAIL_REFERENCE_POINT,
    COMBAT_TAB_REFERENCE_POINT,
    MAIL_BUTTON_REFERENCE_POINT,
    MAIL_BUTTON_REFERENCE_SIZE,
    MAILBOX_REFERENCE_SIZE,
    ProfileWorker,
    READ_ALL_MAIL_REFERENCE_POINT,
)


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded)


def _place(canvas: np.ndarray, template: np.ndarray, left: int, top: int) -> None:
    height, width = template.shape[:2]
    canvas[top : top + height, left : left + width] = template


def _asset(monitor: BrowserMailMonitor, filename: str) -> np.ndarray:
    image = cv2.imread(str(Path(monitor.asset_dir) / filename))
    assert image is not None
    return image


class _TapSession:
    def __init__(self) -> None:
        self.taps: list[tuple[tuple[int, int, int, int], tuple[int, int]]] = []

    def tap_farm_template(
        self, bounds: tuple[int, int, int, int], image_size: tuple[int, int]
    ) -> None:
        self.taps.append((bounds, image_size))


def test_mail_button_uses_the_fixed_reference_coordinate() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    worker._tap_monitor_reference_point(
        MAIL_BUTTON_REFERENCE_POINT,
        MAIL_BUTTON_REFERENCE_SIZE,
        MAIL_BUTTON_REFERENCE_SIZE,
    )

    assert worker.session.taps == [((144, 544, 2, 2), (1259, 672))]


def test_combat_tab_uses_fixed_xy_and_scales_to_the_renderer() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    worker._tap_monitor_reference_point(
        COMBAT_TAB_REFERENCE_POINT,
        MAILBOX_REFERENCE_SIZE,
        (1280, 720),
    )

    assert worker.session.taps == [((94, 260, 2, 2), (1280, 720))]


def test_read_all_and_close_mail_use_fixed_scaled_xy() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    worker._tap_monitor_reference_point(
        READ_ALL_MAIL_REFERENCE_POINT,
        MAILBOX_REFERENCE_SIZE,
        (1280, 720),
    )
    worker._tap_monitor_reference_point(
        CLOSE_MAIL_REFERENCE_POINT,
        MAILBOX_REFERENCE_SIZE,
        (1280, 720),
    )

    assert worker.session.taps == [
        ((256, 651, 2, 2), (1280, 720)),
        ((1195, 76, 2, 2), (1280, 720)),
    ]


def test_fixed_mail_points_scale_for_five_column_viewport() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    for point in (
        COMBAT_TAB_REFERENCE_POINT,
        READ_ALL_MAIL_REFERENCE_POINT,
        CLOSE_MAIL_REFERENCE_POINT,
    ):
        worker._tap_monitor_reference_point(
            point,
            MAILBOX_REFERENCE_SIZE,
            (384, 216),
        )

    assert worker.session.taps == [
        ((27, 77, 2, 2), (384, 216)),
        ((76, 195, 2, 2), (384, 216)),
        ((358, 22, 2, 2), (384, 216)),
    ]


def test_mail_close_is_matched_only_in_its_scoped_region() -> None:
    monitor = BrowserMailMonitor()
    mailbox = np.full((1080, 1920, 3), 120, dtype=np.uint8)
    close = _asset(monitor, "mail_close.png")
    _place(mailbox, close, 1755, 100)
    assert monitor.is_mail_open(_png(mailbox)) is True


def test_only_red_badge_one_beside_combat_category_is_accepted() -> None:
    monitor = BrowserMailMonitor()
    canvas = np.full((1080, 1920, 3), 160, dtype=np.uint8)
    badge_one = _asset(monitor, "combat_unread_one.png")
    _place(canvas, badge_one, 180, 286)
    assert monitor.has_new_combat_mail(_png(canvas)) is True

    outside = np.full_like(canvas, 160)
    _place(outside, badge_one, 600, 286)
    assert monitor.has_new_combat_mail(_png(outside)) is False

    other_red_badge = np.full_like(canvas, 160)
    cv2.circle(other_red_badge, (200, 305), 19, (20, 35, 225), thickness=-1)
    cv2.putText(
        other_red_badge,
        "2",
        (190, 316),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )
    assert monitor.has_new_combat_mail(_png(other_red_badge)) is False


def test_territory_attacked_title_is_matched_in_first_mail_area() -> None:
    monitor = BrowserMailMonitor()
    canvas = np.full((1080, 1920, 3), 205, dtype=np.uint8)
    title = _asset(monitor, "territory_attacked.png")
    _place(canvas, title, 330, 178)
    assert monitor.is_territory_attacked(_png(canvas)) is True

    lower_row = np.full_like(canvas, 205)
    # A matching attack subject in row 2/3 must not authorise an alert when
    # the yellow top card is a different message.
    _place(lower_row, title, 330, 300)
    assert monitor.is_territory_attacked(_png(lower_row)) is False

    unrelated_region = np.full_like(canvas, 205)
    _place(unrelated_region, title, 1200, 700)
    assert monitor.is_territory_attacked(_png(unrelated_region)) is False
