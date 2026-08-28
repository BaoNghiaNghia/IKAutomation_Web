from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from ik_chrome_auto.mail_monitor import BrowserMailMonitor
from ik_chrome_auto.runner import (
    CLOSE_MAIL_POINT,
    COMBAT_TAB_POINT,
    MAIL_BUTTON_POINT,
    MONITOR_REFERENCE_ASPECT_RATIO,
    ProfileWorker,
    READ_ALL_MAIL_POINT,
    FIRST_MAIL_ROW_POINT,
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


class _BaselineMonitor:
    def find_close_button(self, _png: bytes) -> object:
        return object()


class _EventLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, str]]] = []

    def write(self, event: str, payload: dict[str, str]) -> None:
        self.events.append((event, payload))


def test_video_measured_mail_points_use_direct_canvas_percentages() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    for point in (
        MAIL_BUTTON_POINT,
        COMBAT_TAB_POINT,
        READ_ALL_MAIL_POINT,
        FIRST_MAIL_ROW_POINT,
        CLOSE_MAIL_POINT,
    ):
        worker._tap_monitor_viewport_point(point, (1260, 674))

    assert worker.session.taps == [
        ((144, 544, 2, 2), (1260, 674)),
        ((79, 245, 2, 2), (1260, 674)),
        ((252, 613, 2, 2), (1260, 674)),
        ((316, 116, 2, 2), (1260, 674)),
        ((1188, 76, 2, 2), (1260, 674)),
    ]


def test_combat_tab_uses_relative_xy_and_scales_to_the_renderer() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    worker._tap_monitor_viewport_point(COMBAT_TAB_POINT, (1280, 720))

    assert worker.session.taps == [((80, 262, 2, 2), (1280, 720))]


def test_read_all_and_close_mail_use_relative_scaled_xy() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    worker._tap_monitor_viewport_point(READ_ALL_MAIL_POINT, (1280, 720))
    worker._tap_monitor_viewport_point(CLOSE_MAIL_POINT, (1280, 720))

    assert worker.session.taps == [
        ((256, 655, 2, 2), (1280, 720)),
        ((1207, 81, 2, 2), (1280, 720)),
    ]


def test_mail_points_scale_for_five_column_viewport() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    for point in (COMBAT_TAB_POINT, READ_ALL_MAIL_POINT, CLOSE_MAIL_POINT):
        worker._tap_monitor_viewport_point(point, (384, 216))

    assert worker.session.taps == [
        ((23, 78, 2, 2), (384, 216)),
        ((76, 196, 2, 2), (384, 216)),
        ((361, 24, 2, 2), (384, 216)),
    ]


def test_monitor_points_use_a_sixteen_by_nine_reference_but_scale_each_axis() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()

    # 21:9 is intentionally scaled by its live canvas dimensions. This keeps
    # the X/Y percentage stable even when a user has not used the 16:9 default.
    worker._tap_monitor_viewport_point(COMBAT_TAB_POINT, (840, 360))

    assert MONITOR_REFERENCE_ASPECT_RATIO == 16 / 9
    assert worker.session.taps == [((52, 130, 2, 2), (840, 360))]


def test_initial_monitor_pass_reads_all_notifications_before_combat_is_checked() -> None:
    worker = ProfileWorker.__new__(ProfileWorker)
    worker.session = _TapSession()
    worker.profile = SimpleNamespace(id="account-1")
    worker.event_log = _EventLog()
    worker._mail_monitor = _BaselineMonitor()
    worker._capture_mail_canvas = lambda: (b"png", (1260, 674))
    worker._monitor_pause = lambda _seconds: None

    result = worker._check_combat_mail(initial_scan=True)

    assert result == "mail_baseline"
    # Open Mail → Read & Receive All → Close.  Combat is intentionally absent
    # in pass 1 because the baseline must clear every notification category.
    assert worker.session.taps == [
        ((252, 613, 2, 2), (1260, 674)),
        ((1188, 76, 2, 2), (1260, 674)),
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
