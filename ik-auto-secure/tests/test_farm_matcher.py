from __future__ import annotations

import cv2
import numpy as np

from ik_chrome_auto.farm_matcher import BrowserCanvasMatcher
from ik_chrome_auto.farm_vision import FarmTemplateId


def test_matcher_scales_template_and_returns_canvas_relative_bounds(tmp_path) -> None:
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (27, 17), (255, 255, 255), 2)
    cv2.line(template, (0, 19), (29, 0), (80, 160, 220), 2)
    cv2.imwrite(str(tmp_path / "world_map_anchor.png"), template)
    canvas = np.zeros((360, 640, 3), dtype=np.uint8)
    scaled = cv2.resize(template, (15, 10), interpolation=cv2.INTER_AREA)
    canvas[240:250, 100:115] = scaled
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok
    matcher = BrowserCanvasMatcher(tmp_path)
    evidence = matcher._match(cv2.imdecode(encoded, cv2.IMREAD_COLOR), FarmTemplateId.WORLD_MAP_ANCHOR)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (100, 240)
