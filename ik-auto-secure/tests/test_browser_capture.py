import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ik_chrome_auto.browser as browser_module
from ik_chrome_auto.browser import ChromeProfileSession, _image_has_visible_content
from ik_chrome_auto.image_utils import RGBImage, decode_png


ASSET = (
    Path(__file__).parents[1]
    / "src"
    / "ik_chrome_auto"
    / "assets"
    / "browser-capture-reference.png"
)
ASSET_PNG = ASSET.read_bytes()
BOX = {"x": 12.5, "y": 34.25, "width": 500.0, "height": 300.0}


class FakePage:
    def __init__(self, url: str = "https://ik.playfun.vn/play-game") -> None:
        self.url = url
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


class FakeCanvas:
    def __init__(self, direct_png: bytes | None = None) -> None:
        self.direct_png = direct_png or (b"\x89PNG\r\n\x1a\n" + b"\0" * 1_200)
        self.evaluate_calls = 0
        self.screenshot_calls = 0

    def evaluate(self, _script: str) -> str:
        self.evaluate_calls += 1
        encoded = base64.b64encode(self.direct_png).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def screenshot(self, **_kwargs: Any) -> bytes:
        self.screenshot_calls += 1
        return ASSET_PNG


class FakeCDP:
    def __init__(self, *, fail_capture: bool = False) -> None:
        self.fail_capture = fail_capture
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.detach_calls = 0

    def send(self, method: str, params: dict[str, Any]) -> dict[str, str]:
        self.calls.append((method, params))
        if method == "Page.captureScreenshot":
            if self.fail_capture:
                raise RuntimeError("capture unavailable")
            return {"data": base64.b64encode(ASSET_PNG).decode("ascii")}
        return {}

    def detach(self) -> None:
        self.detach_calls += 1


class FakeContext:
    def __init__(self, sessions: list[FakeCDP] | None = None) -> None:
        self.sessions = sessions or [FakeCDP()]
        self.new_session_calls = 0
        self.close_calls = 0
        self.pages: list[FakePage] = []

    def new_cdp_session(self, _page: FakePage) -> FakeCDP:
        index = self.new_session_calls
        self.new_session_calls += 1
        if index >= len(self.sessions):
            self.sessions.append(FakeCDP())
        return self.sessions[index]

    def close(self) -> None:
        self.close_calls += 1


def make_session(
    *,
    canvas: FakeCanvas | None = None,
    context: FakeContext | None = None,
) -> tuple[ChromeProfileSession, FakeCanvas, FakeContext, FakePage]:
    session = ChromeProfileSession.__new__(ChromeProfileSession)
    canvas = canvas or FakeCanvas()
    context = context or FakeContext()
    page = FakePage()
    context.pages = [page]
    session.config = SimpleNamespace(
        browser=SimpleNamespace(startup_timeout_ms=90_000, headless=True),
    )
    session._page = page
    session._context = context
    session._browser = None
    session._playwright = None
    session._managed = False
    session._closing = False
    session._window_handle = None
    session._configured_frames = {}
    session._tracked_pages = set()
    session._runtime_page = None
    session._page_cdp_session = None
    session._direct_canvas_capture_supported = None
    session.find_frame = lambda: object()
    session._largest_canvas = lambda _frame: (canvas, BOX.copy())
    return session, canvas, context, page


def test_solid_black_webgl_capture_is_rejected() -> None:
    black = RGBImage(320, 180, bytes(320 * 180 * 3))
    assert not _image_has_visible_content(black)


def test_rendered_game_capture_is_accepted() -> None:
    assert _image_has_visible_content(decode_png(ASSET.read_bytes()))


def test_black_direct_capture_is_probed_only_once() -> None:
    session, canvas, context, _page = make_session()

    first, _ = session.capture_game_surface_png()
    second, _ = session.capture_game_surface_png()

    assert first == ASSET_PNG
    assert second == ASSET_PNG
    assert canvas.evaluate_calls == 1
    assert canvas.screenshot_calls == 0
    assert context.new_session_calls == 1
    assert session._direct_canvas_capture_supported is False


def test_headed_windows_capture_uses_profile_cdp_not_desktop_capture(monkeypatch: Any) -> None:
    session, canvas, context, _page = make_session()
    session.config.browser.headless = False
    monkeypatch.setattr(browser_module.sys, "platform", "win32")

    png, _ = session.capture_game_surface_png()

    assert png == ASSET_PNG
    assert context.new_session_calls == 1
    assert canvas.evaluate_calls == 0
    assert canvas.screenshot_calls == 0


def test_farm_capture_uses_cdp_when_headed(monkeypatch: Any) -> None:
    session, canvas, context, _page = make_session()
    session.config.browser.headless = False
    session._direct_canvas_capture_supported = False
    monkeypatch.setattr(browser_module.sys, "platform", "win32")

    png, _ = session.capture_game_surface_png(prefer_browser_capture=True)

    assert png == ASSET_PNG
    assert context.new_session_calls == 1
    assert canvas.screenshot_calls == 0


def test_farm_state_uses_visible_capture_when_headed() -> None:
    session, _canvas, _context, _page = make_session()
    session.config.browser.headless = False
    capture_preferences: list[bool] = []
    session.capture_game_surface_png = lambda *, prefer_browser_capture: (
        capture_preferences.append(prefer_browser_capture) or (ASSET_PNG, BOX.copy())
    )
    session._farm_matcher = SimpleNamespace(detect=lambda _png: "detected")

    detected, surface, image_size = session.detect_farm_state()

    assert detected == "detected"
    assert capture_preferences == [False]
    assert surface == BOX
    assert image_size == (333, 322)


def test_farm_state_keeps_browser_capture_when_headless() -> None:
    session, _canvas, _context, _page = make_session()
    capture_preferences: list[bool] = []
    session.capture_game_surface_png = lambda *, prefer_browser_capture: (
        capture_preferences.append(prefer_browser_capture) or (ASSET_PNG, BOX.copy())
    )
    session._farm_matcher = SimpleNamespace(detect=lambda _png: "detected")

    session.detect_farm_state()

    assert capture_preferences == [True]


def test_scroll_game_surface_sends_wheel_at_canvas_centre() -> None:
    session, _canvas, context, _page = make_session()

    session.scroll_game_surface(480)

    assert context.new_session_calls == 1
    method, params = context.sessions[0].calls[0]
    assert method == "Input.dispatchMouseEvent"
    assert params == {
        "type": "mouseWheel",
        "x": BOX["x"] + BOX["width"] / 2,
        "y": BOX["y"] + BOX["height"] / 2,
        "deltaX": 0,
        "deltaY": 480.0,
        "modifiers": 0,
        "pointerType": "mouse",
    }


def test_escape_is_dispatched_without_focusing_real_window() -> None:
    session, _canvas, context, _page = make_session()

    session.press_escape()

    calls = context.sessions[0].calls
    assert [method for method, _params in calls] == [
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    assert [params["type"] for _method, params in calls] == ["keyDown", "keyUp"]
    assert all(params["windowsVirtualKeyCode"] == 27 for _method, params in calls)


def test_mouse_fallback_click_uses_the_template_center() -> None:
    session, _canvas, context, _page = make_session()

    session.click_farm_template_mouse((10, 20, 30, 40), (100, 100))

    calls = context.sessions[0].calls
    assert [method for method, _params in calls] == ["Input.dispatchMouseEvent"] * 3
    assert [params["type"] for _method, params in calls] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert all(params["x"] == 137.5 and params["y"] == 154.25 for _method, params in calls)


def test_cdp_capture_uses_exact_canvas_clip() -> None:
    session, canvas, context, _page = make_session()
    session._direct_canvas_capture_supported = False

    png, returned_box = session.capture_game_surface_png()

    method, params = context.sessions[0].calls[0]
    assert method == "Page.captureScreenshot"
    assert params == {
        "format": "png",
        "fromSurface": True,
        "captureBeyondViewport": False,
        "clip": {**BOX, "scale": 1},
    }
    assert png == ASSET_PNG
    assert returned_box == BOX
    assert canvas.screenshot_calls == 0


def test_locator_screenshot_is_only_the_last_fallback() -> None:
    context = FakeContext([FakeCDP(fail_capture=True)])
    session, canvas, _context, _page = make_session(context=context)
    session._direct_canvas_capture_supported = False

    png, _ = session.capture_game_surface_png()

    assert png == ASSET_PNG
    assert canvas.screenshot_calls == 1
    assert context.new_session_calls == 1


def test_close_detaches_page_cdp_even_for_external_profile() -> None:
    session, _canvas, context, page = make_session()
    cdp = session._get_page_cdp_session(page)

    session.close()

    assert cdp.detach_calls == 1
    assert context.close_calls == 0
    assert session._page_cdp_session is None
    assert session._runtime_page is None


def test_switching_page_detaches_old_session_and_resets_capture_probe() -> None:
    context = FakeContext([FakeCDP(), FakeCDP()])
    session, _canvas, _context, old_page = make_session(context=context)
    old_cdp = session._get_page_cdp_session(old_page)
    session._direct_canvas_capture_supported = False
    new_page = FakePage("https://ik.playfun.vn/play-game?new=1")

    new_cdp = session._get_page_cdp_session(new_page)

    assert old_cdp.detach_calls == 1
    assert new_cdp is context.sessions[1]
    assert context.new_session_calls == 2
    assert session._runtime_page is new_page
    assert session._direct_canvas_capture_supported is None
