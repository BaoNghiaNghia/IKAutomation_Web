from __future__ import annotations

import cv2
import numpy as np

from ik_chrome_auto import browser as browser_module
from ik_chrome_auto.cdp_ports import (
    command_line_remote_debugging_port,
    command_line_user_data_dir,
    stable_profile_cdp_port,
)
from ik_chrome_auto.config import _legacy_cdp_port
from ik_chrome_auto.input_engine import ProfileInputEngine, ViewportPoint
from ik_chrome_auto.mail_monitor import (
    COMBAT_BADGE_CAPTURE_REGION,
    MAIL_BADGE_CAPTURE_REGION,
    TERRITORY_CAPTURE_REGION,
    BrowserMailMonitor,
)
from ik_chrome_auto.surface import fixed_game_surface_box, origin_surface_box


class RecordingCDP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send(self, method: str, params: dict[str, object]) -> None:
        self.calls.append((method, params))


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded)


def test_region_badge_matches_full_canvas_path() -> None:
    monitor = BrowserMailMonitor()
    template = cv2.imread(str(monitor.asset_dir / "mail_unread_one.png"))
    assert template is not None
    canvas = np.full((720, 1280, 3), 160, dtype=np.uint8)
    canvas[530 : 530 + template.shape[0], 155 : 155 + template.shape[1]] = template
    assert monitor.has_new_mail(_png(canvas)) is True

    left, top, right, bottom = MAIL_BADGE_CAPTURE_REGION
    crop = canvas[int(720 * top) : int(720 * bottom), int(1280 * left) : int(1280 * right)]
    assert monitor.has_new_mail_in_region(
        _png(crop), (1280, 720), MAIL_BADGE_CAPTURE_REGION, 1.0
    ) is True

    blank = np.full_like(crop, 160)
    assert monitor.has_new_mail_in_region(
        _png(blank), (1280, 720), MAIL_BADGE_CAPTURE_REGION, 1.0
    ) is False


def test_region_capture_constants_expand_their_roi() -> None:
    assert MAIL_BADGE_CAPTURE_REGION[0] <= 0.07
    assert COMBAT_BADGE_CAPTURE_REGION[0] <= 0.068
    assert TERRITORY_CAPTURE_REGION[0] <= 0.14


def test_repeat_click_at_same_point_skips_redundant_move() -> None:
    cdp = RecordingCDP()
    ProfileInputEngine.click(cdp, ViewportPoint(100.0, 100.0), kind="mouse")
    assert [params["type"] for _method, params in cdp.calls] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    cdp.calls.clear()
    ProfileInputEngine.click(cdp, ViewportPoint(100.0, 100.0), kind="mouse")
    assert [params["type"] for _method, params in cdp.calls] == [
        "mousePressed",
        "mouseReleased",
    ]
    cdp.calls.clear()
    ProfileInputEngine.click(cdp, ViewportPoint(101.0, 100.0), kind="mouse")
    assert [params["type"] for _method, params in cdp.calls][0] == "mouseMoved"


def test_surface_and_cdp_helpers_stay_canonical() -> None:
    assert fixed_game_surface_box() == {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0}
    assert origin_surface_box({"x": 8.0, "y": 9.0, "width": 640.0, "height": 360.0}) == {
        "x": 0.0,
        "y": 0.0,
        "width": 640.0,
        "height": 360.0,
    }
    assert browser_module._fixed_game_surface_box() == fixed_game_surface_box()
    assert stable_profile_cdp_port("main") == _legacy_cdp_port("main")
    assert browser_module._profile_cdp_port("main") == _legacy_cdp_port("main")
    assert (
        command_line_user_data_dir('--user-data-dir="D:/IK/p1" --remote-debugging-port=9222')
        == "D:/IK/p1"
    )
    assert command_line_remote_debugging_port("--remote-debugging-port=9222") == 9222
