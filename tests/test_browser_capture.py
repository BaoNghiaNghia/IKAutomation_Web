import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ik_chrome_auto.browser as browser_module
from ik_chrome_auto.browser import (
    GAME_FRAME_FIT_SCRIPT,
    ChromeProfileSession,
    _image_has_visible_content,
    _low_gpu_init_script,
)
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


def test_fps_limiter_can_update_a_retained_profile_to_22_fps() -> None:
    script = _low_gpu_init_script(22)

    assignment = "window.__IK_RENDER_INTERVAL_MS = 45.4545;"
    assert assignment in script
    assert script.index(assignment) < script.index("if (window.__IK_LOW_GPU_MODE) return")
    assert "Number(window.__IK_RENDER_INTERVAL_MS)" in script


def test_game_frame_fit_overrides_the_live_gtarcade_iframe_inline_size() -> None:
    assert "iframe.iframe" in GAME_FRAME_FIT_SCRIPT
    assert "union.gtarcade.com/channel/" in GAME_FRAME_FIT_SCRIPT
    assert "position: fixed !important" in GAME_FRAME_FIT_SCRIPT
    assert "width: 100vw !important" in GAME_FRAME_FIT_SCRIPT
    assert "height: 100vh !important" in GAME_FRAME_FIT_SCRIPT


def test_game_frame_fit_removes_the_eight_pixel_body_margin_inside_the_frame() -> None:
    assert "location.hostname.endsWith('.gtarcade.com')" in GAME_FRAME_FIT_SCRIPT
    assert "node.style.setProperty('margin', '0', 'important')" in GAME_FRAME_FIT_SCRIPT
    assert "node.style.setProperty('padding', '0', 'important')" in GAME_FRAME_FIT_SCRIPT
    assert "document.querySelector(iframeSelector)" in GAME_FRAME_FIT_SCRIPT
    assert "MutationObserver" in GAME_FRAME_FIT_SCRIPT


class FakePage:
    def __init__(self, url: str = "https://ik.playfun.vn/play-game") -> None:
        self.url = url
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


def test_focused_farm_input_is_read_from_the_inner_game_frame() -> None:
    class Root:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def evaluate(self, _expression: str) -> str | None:
            return self.value

    session = ChromeProfileSession.__new__(ChromeProfileSession)
    outer = Root(None)
    game = Root("564")
    session._frame_roots = lambda: [outer, game]

    assert session._focused_farm_input_value() == "564"


class FakeCanvas:
    def __init__(self, direct_png: bytes | None = None) -> None:
        self.direct_png = direct_png or (b"\x89PNG\r\n\x1a\n" + b"\0" * 1_200)
        self.evaluate_calls = 0
        self.screenshot_calls = 0
        self.click_calls: list[dict[str, Any]] = []

    def evaluate(self, _script: str) -> str:
        self.evaluate_calls += 1
        encoded = base64.b64encode(self.direct_png).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def screenshot(self, **_kwargs: Any) -> bytes:
        self.screenshot_calls += 1
        return ASSET_PNG

    def click(self, **kwargs: Any) -> None:
        self.click_calls.append(kwargs)


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


def test_synced_printable_keyboard_event_is_dispatched_with_text_and_modifiers() -> None:
    session, _canvas, context, _page = make_session()

    session.apply_synced_input({
        "type": "keydown",
        "keyboard": {
            "key": "A",
            "code": "KeyA",
            "key_code": 65,
            "location": 0,
            "repeat": False,
            "shift": True,
            "ctrl": False,
            "alt": False,
            "meta": False,
        },
    })

    method, params = context.sessions[0].calls[0]
    assert method == "Input.dispatchKeyEvent"
    assert params["type"] == "keyDown"
    assert params["key"] == "A"
    assert params["code"] == "KeyA"
    assert params["windowsVirtualKeyCode"] == 65
    assert params["modifiers"] == 8
    assert params["text"] == "A"


def test_synced_ctrl_shortcut_does_not_inject_printable_text() -> None:
    session, _canvas, context, _page = make_session()

    session.apply_synced_input({
        "type": "keydown",
        "keyboard": {
            "key": "c",
            "code": "KeyC",
            "key_code": 67,
            "ctrl": True,
        },
    })

    _method, params = context.sessions[0].calls[0]
    assert params["modifiers"] == 2
    assert "text" not in params


def test_sync_probe_is_rearmed_for_a_retained_browser_frame() -> None:
    class RetainedFrame:
        url = "https://ik.playfun.vn/play-game"

        def __init__(self) -> None:
            self.arguments: list[list[bool]] = []

        def evaluate(self, _script: str, argument: list[bool]) -> bool:
            self.arguments.append(argument)
            return True

    session = ChromeProfileSession.__new__(ChromeProfileSession)
    frame = RetainedFrame()
    session._page = SimpleNamespace(frames=[frame], is_closed=lambda: False)
    session._sync_source = True
    session._inspector_enabled = False
    session._drag_item_visible = False
    session._scrollbars_visible = False
    session._configured_frames = {}

    armed = session._repair_and_count_sync_frames()

    assert armed == 1
    assert frame.arguments == [[True, False]]
    assert id(frame) in session._configured_frames


def test_unchanged_follower_sync_mode_does_not_reconfigure_slow_frames() -> None:
    session = ChromeProfileSession.__new__(ChromeProfileSession)
    session._sync_source = False
    calls: list[bool] = []
    session._configure_interaction_frames = lambda **_kwargs: calls.append(True)

    assert session.set_sync_source(False) == 0
    assert calls == []


def test_synced_pointer_falls_back_to_viewport_while_canvas_is_navigating() -> None:
    session = ChromeProfileSession.__new__(ChromeProfileSession)
    session._page = SimpleNamespace(
        viewport_size={"width": 640, "height": 360},
        is_closed=lambda: False,
    )
    session._frame_for_input = lambda _event: (_ for _ in ()).throw(
        RuntimeError("frame detached")
    )

    box = session._synced_pointer_target_box({"canvas": {"index": 0}})

    assert box == {"x": 0.0, "y": 0.0, "width": 640.0, "height": 360.0}


def test_synced_pointer_uses_largest_target_canvas_not_source_canvas_index() -> None:
    class Locator:
        def __init__(self) -> None:
            self.boxes = [
                {"x": 4.0, "y": 5.0, "width": 32.0, "height": 32.0},
                {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0},
            ]

        def count(self) -> int:
            return len(self.boxes)

        def nth(self, index: int) -> Any:
            return SimpleNamespace(bounding_box=lambda: self.boxes[index])

    frame = SimpleNamespace(locator=lambda _selector: Locator())
    session = ChromeProfileSession.__new__(ChromeProfileSession)

    box = session._canvas_box(frame, 0)

    assert box == {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0}


def test_native_window_falls_back_to_managed_chrome_process(monkeypatch: Any) -> None:
    session = ChromeProfileSession.__new__(ChromeProfileSession)
    session.profile = SimpleNamespace(name="Tài khoản 02 · Vochon***")
    session._managed_browser_pid = 4321
    monkeypatch.setattr(browser_module, "find_chrome_window", lambda _title: None)
    monkeypatch.setattr(
        browser_module,
        "find_chrome_window_for_process",
        lambda process_id: 9876 if process_id == 4321 else None,
    )

    assert session._find_native_window() == 9876


def test_synced_canvas_event_selects_largest_matching_game_frame() -> None:
    class Locator:
        def __init__(self, boxes: list[dict[str, float]]) -> None:
            self.boxes = boxes

        def count(self) -> int:
            return len(self.boxes)

        def nth(self, index: int) -> Any:
            return SimpleNamespace(bounding_box=lambda: self.boxes[index])

    class Frame:
        def __init__(self, url: str, boxes: list[dict[str, float]]) -> None:
            self.url = url
            self.boxes = boxes

        def locator(self, _selector: str) -> Locator:
            return Locator(self.boxes)

    wrapper = Frame(
        "https://game.example/frame",
        [{"x": 0.0, "y": 0.0, "width": 20.0, "height": 20.0}],
    )
    game = Frame(
        "https://game.example/frame",
        [{"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0}],
    )

    resolved = ChromeProfileSession._best_input_frame(
        [wrapper, game],
        {"type": "pointerdown", "canvas": {"ratio_x": 0.5, "ratio_y": 0.5}},
    )

    assert resolved is game


def test_find_frame_discovers_canvas_in_nested_iframe_locator() -> None:
    class CanvasList:
        def __init__(self, boxes: list[dict[str, float]]) -> None:
            self.boxes = boxes

        def count(self) -> int:
            return len(self.boxes)

        def nth(self, index: int) -> Any:
            return SimpleNamespace(bounding_box=lambda: self.boxes[index])

    class IframeList:
        def __init__(self, children: list[Any]) -> None:
            self.children = children

        def count(self) -> int:
            return len(self.children)

        def nth(self, index: int) -> Any:
            return SimpleNamespace(content_frame=self.children[index])

    class Root:
        def __init__(
            self,
            *,
            url: str = "",
            canvases: list[dict[str, float]] | None = None,
            children: list[Any] | None = None,
        ) -> None:
            self.url = url
            self.canvases = canvases or []
            self.children = children or []

        def locator(self, selector: str) -> Any:
            if selector == "canvas":
                return CanvasList(self.canvases)
            if selector == "iframe":
                return IframeList(self.children)
            raise AssertionError(selector)

    game = Root(canvases=[{"x": 8.0, "y": 8.0, "width": 1280.0, "height": 720.0}])
    login = Root(children=[game])
    main = Root(url="https://ik.playfun.vn/play-game", children=[login])
    main.frames = [main, login]
    main.main_frame = main
    main.is_closed = lambda: False
    session = ChromeProfileSession.__new__(ChromeProfileSession)
    session._page = main

    assert session.find_frame() is game


def test_mouse_fallback_click_uses_the_template_center() -> None:
    session, _canvas, context, _page = make_session()

    session.click_farm_template_mouse((10, 20, 30, 40), (100, 100))

    assert context.sessions[0].calls == []
    assert _canvas.click_calls == [{
        "position": {
            "x": BOX["width"] * 0.25,
            "y": BOX["height"] * 0.4,
        },
        "force": True,
        "timeout": session.config.browser.startup_timeout_ms,
    }]


def test_canvas_ratio_mouse_click_uses_locator_in_the_correct_frame() -> None:
    session, _canvas, context, _page = make_session()

    session.click_game_surface_ratio(53 / 1280, 666 / 720)

    assert context.sessions[0].calls == []
    assert _canvas.click_calls == [{
        "position": {
            "x": BOX["width"] * 53 / 1280,
            "y": BOX["height"] * 666 / 720,
        },
        "force": True,
        "timeout": 90_000,
    }]


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


def test_cdp_capture_uses_composited_viewport_when_canvas_is_detached() -> None:
    session, _canvas, context, page = make_session()
    page.viewport_size = {"width": 1280, "height": 720}
    session._largest_canvas = lambda _frame: (_ for _ in ()).throw(
        RuntimeError("Không tìm thấy canvas game đang hiển thị")
    )

    png, returned_box = session.capture_game_surface_png()

    method, params = context.sessions[0].calls[0]
    assert method == "Page.captureScreenshot"
    assert params == {
        "format": "png",
        "fromSurface": True,
        "captureBeyondViewport": False,
    }
    assert png == ASSET_PNG
    assert returned_box == {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0}


def test_cdp_capture_removes_composited_body_gutter_when_canvas_is_detached() -> None:
    session, _canvas, context, page = make_session()
    page.viewport_size = {"width": 1296, "height": 736}
    session._largest_canvas = lambda _frame: (_ for _ in ()).throw(
        RuntimeError("Không tìm thấy canvas game đang hiển thị")
    )

    png, returned_box = session.capture_game_surface_png()

    method, params = context.sessions[0].calls[0]
    assert method == "Page.captureScreenshot"
    assert params["clip"] == {
        "x": 8.0,
        "y": 8.0,
        "width": 1280.0,
        "height": 720.0,
        "scale": 1,
    }
    assert png == ASSET_PNG
    assert returned_box == {"x": 8.0, "y": 8.0, "width": 1280.0, "height": 720.0}


def test_cdp_capture_keeps_full_viewport_after_iframe_fit_css_is_active() -> None:
    session, _canvas, context, page = make_session()
    page.viewport_size = {"width": 1296, "height": 736}
    page.evaluate = lambda _script: True
    session._largest_canvas = lambda _frame: (_ for _ in ()).throw(
        RuntimeError("Không tìm thấy canvas game đang hiển thị")
    )

    png, returned_box = session.capture_game_surface_png()

    method, params = context.sessions[0].calls[0]
    assert method == "Page.captureScreenshot"
    assert "clip" not in params
    assert png == ASSET_PNG
    assert returned_box == {"x": 0.0, "y": 0.0, "width": 1296.0, "height": 736.0}


def test_canvas_ratio_click_uses_inferred_compositor_game_gutter() -> None:
    session, _canvas, context, page = make_session()
    page.viewport_size = {"width": 1296, "height": 736}
    session._largest_canvas = lambda _frame: (_ for _ in ()).throw(
        RuntimeError("Không tìm thấy canvas game đang hiển thị")
    )

    session.click_game_surface_ratio(0.25, 0.75)

    assert context.sessions[0].calls == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": 328.0, "y": 548.0, "button": "left", "clickCount": 1},
        ),
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": 328.0, "y": 548.0, "button": "left", "clickCount": 1},
        ),
    ]


def test_canvas_ratio_click_uses_compositor_when_canvas_is_detached() -> None:
    session, _canvas, context, page = make_session()
    page.viewport_size = {"width": 1280, "height": 720}
    session._largest_canvas = lambda _frame: (_ for _ in ()).throw(
        RuntimeError("Không tìm thấy canvas game đang hiển thị")
    )

    session.click_game_surface_ratio(0.25, 0.75)

    assert context.sessions[0].calls == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": 320.0, "y": 540.0, "button": "left", "clickCount": 1},
        ),
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": 320.0, "y": 540.0, "button": "left", "clickCount": 1},
        ),
    ]


def test_monitor_capture_uses_only_normalized_canvas_region() -> None:
    session, _canvas, context, _page = make_session()

    png, full_size, scale = session.capture_game_region_png(
        (0.2, 0.3, 0.8, 0.7),
        scale=0.65,
    )

    method, params = context.sessions[0].calls[0]
    assert method == "Page.captureScreenshot"
    assert params["clip"] == {
        "x": 112.5,
        "y": 124.25,
        "width": 300.0,
        "height": 120.0,
        "scale": 0.65,
    }
    assert png == ASSET_PNG
    assert full_size == (500, 300)
    assert scale == 0.65


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


def test_close_browser_closes_detached_external_profile_context() -> None:
    session, _canvas, context, page = make_session()
    cdp = session._get_page_cdp_session(page)

    session.close(close_browser=True)

    assert cdp.detach_calls == 1
    assert ("Browser.close", {}) in cdp.calls
    assert context.close_calls == 1


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
