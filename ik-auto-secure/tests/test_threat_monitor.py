from pathlib import Path

import cv2
import numpy as np

from ik_chrome_auto.threat_monitor import (
    INCOMING_ATTACK,
    INVESTIGATED,
    BrowserThreatMonitor,
)


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded)


def _place(canvas: np.ndarray, template: np.ndarray, left: int, top: int) -> None:
    height, width = template.shape[:2]
    canvas[top : top + height, left : left + width] = template


def test_threat_monitor_detects_both_events_in_their_scoped_regions() -> None:
    monitor = BrowserThreatMonitor()
    canvas = np.full((1182, 2560, 3), 90, dtype=np.uint8)
    investigated = cv2.imread(str(monitor.asset_dir / "investigated.png"))
    incoming = cv2.imread(str(monitor.asset_dir / "incoming_attack_prefix.png"))
    assert investigated is not None and incoming is not None
    _place(canvas, investigated, 400, 620)
    _place(canvas, incoming, 870, 520)

    events = {match.event for match in monitor.detect(_png(canvas))}

    assert events == {INVESTIGATED, INCOMING_ATTACK}


def test_threat_monitor_does_not_scan_unrelated_screen_regions() -> None:
    monitor = BrowserThreatMonitor()
    canvas = np.full((1182, 2560, 3), 90, dtype=np.uint8)
    investigated = cv2.imread(str(monitor.asset_dir / "investigated.png"))
    incoming = cv2.imread(str(monitor.asset_dir / "incoming_attack_prefix.png"))
    assert investigated is not None and incoming is not None
    _place(canvas, investigated, 1900, 70)
    _place(canvas, incoming, 50, 900)

    assert monitor.detect(_png(canvas)) == ()


def test_threat_monitor_matches_an_already_cropped_downscaled_region() -> None:
    monitor = BrowserThreatMonitor()
    template = cv2.imread(str(monitor.asset_dir / "incoming_attack_prefix.png"))
    assert template is not None
    scaled = cv2.resize(
        template,
        (
            round(template.shape[1] * 0.65),
            round(template.shape[0] * 0.65),
        ),
        interpolation=cv2.INTER_AREA,
    )

    match = monitor.detect_region(
        _png(scaled),
        INCOMING_ATTACK,
        (2560, 1182),
        0.65,
    )

    assert match is not None
    assert match.event == INCOMING_ATTACK
