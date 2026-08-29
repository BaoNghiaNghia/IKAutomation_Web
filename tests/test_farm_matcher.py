from __future__ import annotations

import cv2
import numpy as np

from ik_chrome_auto.farm_matcher import BrowserCanvasMatcher


def test_city_template_search_is_limited_to_bottom_left_corner() -> None:
    assert BrowserCanvasMatcher._region("city_corner", 835, 432) == (0, 324, 139, 108)
from ik_chrome_auto.farm_vision import FarmTemplateId


def test_browser_map_toggle_templates_match_their_actual_direction() -> None:
    from ik_chrome_auto.farm_matcher import SPECS

    assert SPECS[FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON].filename == "browser_map_to_city_tight.png"
    assert SPECS[FarmTemplateId.BROWSER_MAP_TO_CITY_BUTTON].filename == "browser_city_icon_green_tight.png"


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


def test_matcher_scales_height_independently_for_stretched_browser_canvas(tmp_path) -> None:
    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (27, 17), (255, 255, 255), 2)
    cv2.line(template, (0, 19), (29, 0), (80, 160, 220), 2)
    cv2.imwrite(str(tmp_path / "world_map_anchor.png"), template)
    # 640x300 has different horizontal and vertical scale factors from the
    # 1280x720 template source, as happens with a browser-hosted canvas.
    canvas = np.zeros((300, 640, 3), dtype=np.uint8)
    scaled = cv2.resize(template, (15, 8), interpolation=cv2.INTER_AREA)
    canvas[220:228, 100:115] = scaled
    matcher = BrowserCanvasMatcher(tmp_path)
    evidence = matcher._match(canvas, FarmTemplateId.WORLD_MAP_ANCHOR)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (100, 220)


def test_matcher_honours_browser_template_reference_dimensions(tmp_path, monkeypatch) -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    template = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (27, 17), (255, 255, 255), 2)
    cv2.imwrite(str(tmp_path / "browser-city.png"), template)
    monkeypatch.setitem(
        matcher_module.SPECS,
        FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
        matcher_module.TemplateSpec("browser-city.png", threshold=0.78, region="lower_left", reference_width=800, reference_height=400),
    )
    canvas = np.zeros((200, 400, 3), dtype=np.uint8)
    scaled = cv2.resize(template, (15, 10), interpolation=cv2.INTER_AREA)
    canvas[150:160, 90:105] = scaled
    evidence = BrowserCanvasMatcher(tmp_path)._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (90, 150)


def test_matcher_uses_the_best_city_skin_variant(tmp_path, monkeypatch) -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    green = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(green, (2, 2), (27, 17), (0, 220, 0), 2)
    cv2.line(green, (0, 19), (29, 0), (0, 120, 0), 2)
    snow = np.zeros((20, 30, 3), dtype=np.uint8)
    cv2.rectangle(snow, (2, 2), (27, 17), (220, 180, 120), 2)
    cv2.line(snow, (0, 0), (29, 19), (255, 255, 255), 2)
    cv2.imwrite(str(tmp_path / "green.png"), green)
    cv2.imwrite(str(tmp_path / "snow.png"), snow)
    monkeypatch.setitem(
        matcher_module.SPECS,
        FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
        matcher_module.TemplateSpec("green.png", threshold=0.78, region="lower_left", reference_width=400, reference_height=200, alternatives=("snow.png",)),
    )
    canvas = np.zeros((200, 400, 3), dtype=np.uint8)
    canvas[150:170, 90:120] = snow
    evidence = BrowserCanvasMatcher(tmp_path)._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (90, 150)


def test_city_edge_match_is_not_dependent_on_environment_colour(tmp_path, monkeypatch) -> None:
    import ik_chrome_auto.farm_matcher as matcher_module

    city = np.zeros((24, 28, 3), dtype=np.uint8)
    cv2.circle(city, (14, 13), 10, (40, 180, 40), 2)
    cv2.rectangle(city, (9, 6), (19, 18), (50, 210, 70), 2)
    # Same shape, intentionally different night/snow palette.
    night_city = np.zeros((24, 28, 3), dtype=np.uint8)
    cv2.circle(night_city, (14, 13), 10, (190, 80, 210), 2)
    cv2.rectangle(night_city, (9, 6), (19, 18), (225, 95, 230), 2)
    cv2.imwrite(str(tmp_path / "city.png"), city)
    monkeypatch.setitem(
        matcher_module.SPECS,
        FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON,
        matcher_module.TemplateSpec(
            "city.png", threshold=0.55, region="lower_left",
            reference_width=400, reference_height=200, edge=True,
        ),
    )
    canvas = np.zeros((200, 400, 3), dtype=np.uint8)
    canvas[150:174, 90:118] = night_city
    evidence = BrowserCanvasMatcher(tmp_path)._match(canvas, FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON)
    assert evidence.found
    assert evidence.bounds is not None
    assert evidence.bounds[:2] == (90, 150)
