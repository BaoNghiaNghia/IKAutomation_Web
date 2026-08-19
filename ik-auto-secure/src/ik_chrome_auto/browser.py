from __future__ import annotations

import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ik_chrome_auto.interaction import (
    INTERACTION_PROBE,
    calculate_target_point,
    validate_viewport,
)
from ik_chrome_auto.game2048 import RGBImage, decode_png
from ik_chrome_auto.config import is_allowed_url
from ik_chrome_auto.models import AppConfig, ProfileConfig, ProfileMode
from ik_chrome_auto.reader import GameDataReader, redact_url
from ik_chrome_auto.windows import (
    WindowRect,
    capture_screen_region_png,
    find_chrome_window,
    get_window_rect,
    get_renderer_rect,
    is_region_visible_for_window,
    is_window,
    move_window_position,
    move_window_renderer,
    set_taskbar_group,
    set_topmost as set_native_topmost,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright


KNOWN_CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

LOGIN_USERNAME_SELECTORS = (
    "input[autocomplete='username']",
    "input[type='email']",
    "input[name*='email' i]",
    "input[name*='user' i]",
    "input[placeholder*='Email' i]",
    "input[placeholder*='Tên đăng nhập' i]",
)
LOGIN_PASSWORD_SELECTORS = (
    "input[autocomplete='current-password']",
    "input[type='password']",
)


def _image_has_visible_content(image: RGBImage) -> bool:
    """Reject empty WebGL toDataURL captures (transparent/solid black)."""
    step_x = max(1, image.width // 32)
    step_y = max(1, image.height // 24)
    samples = visible = colourful = 0
    for y in range(step_y // 2, image.height, step_y):
        for x in range(step_x // 2, image.width, step_x):
            red, green, blue = image.pixel(x, y)
            samples += 1
            if max(red, green, blue) >= 28:
                visible += 1
            if max(red, green, blue) - min(red, green, blue) >= 14:
                colourful += 1
    return samples > 0 and visible / samples >= 0.08 and colourful / samples >= 0.03


def _png_has_visible_content(png: bytes) -> bool:
    try:
        return _image_has_visible_content(decode_png(png))
    except Exception:
        return False


def find_chrome(configured: str = "auto") -> Path | None:
    if configured and configured.lower() != "auto":
        candidate = Path(os.path.expandvars(configured)).expanduser()
        return candidate if candidate.exists() else None
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = list(KNOWN_CHROME_PATHS)
    if local_app_data:
        candidates.append(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return next((path for path in candidates if path.exists()), None)


class ChromeProfileSession:
    """Owns one Playwright runtime and one Chrome profile on a single worker thread."""

    def __init__(self, config: AppConfig, profile: ProfileConfig) -> None:
        self.config = config
        self.profile = profile
        self.reader = GameDataReader(profile.id, config.data_dir, config.capture)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._managed = profile.mode == ProfileMode.MANAGED
        self._sync_source = False
        self._inspector_enabled = False
        self._drag_item_visible = True
        self._scrollbars_visible = False
        self._window_handle: int | None = None
        self._topmost = False
        self._configured_frames: dict[int, str] = {}
        self._externally_closed = False
        self._closing = False
        self._tracked_pages: set[int] = set()
        # A locator screenshot first scrolls/checks the element and WebGL
        # toDataURL performs a synchronous GPU readback.  Both operations can
        # make a headed game canvas briefly show an old compositor frame.  We
        # therefore keep one page-level CDP session for both screenshots and
        # touch input, and remember when direct canvas capture is unsupported.
        self._runtime_page: Page | None = None
        self._page_cdp_session: Any | None = None
        self._direct_canvas_capture_supported: bool | None = None
        self._farm_matcher: Any | None = None
        self._last_farm_capture_png: bytes | None = None

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Chrome profile chưa được mở")
        return self._context

    @property
    def page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = self._choose_page()
        return self._page

    def start(self, *, navigate: bool = True) -> Page:
        if self._context is not None:
            if navigate:
                self.goto()
            return self.page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError("Thiếu Playwright. Hãy chạy setup.cmd") from error

        self._playwright = sync_playwright().start()
        if self.profile.mode == ProfileMode.MANAGED:
            self._start_managed()
        else:
            self._start_cdp()
        self._attach_lifecycle()
        self.context.add_init_script(INTERACTION_PROBE)
        if self.config.browser.profile_title:
            self.context.add_init_script(self._profile_title_script())
        self.reader.attach(self.context)
        self._page = self._choose_page(create=True)
        self._close_unused_blank_pages(self._page)
        self._track_page(self._page)
        self._configure_interaction_frames(force=True)
        self._apply_profile_title()
        if self.config.browser.auto_resize:
            self.resize(
                self.config.browser.viewport_width,
                self.config.browser.viewport_height,
            )
        if navigate and not self._is_target(self.page.url):
            self.goto()
        elif navigate:
            self.auto_login_if_needed()
        self._bind_native_window()
        return self.page

    def _start_managed(self) -> None:
        assert self._playwright is not None
        if self.profile.user_data_dir is None:
            raise RuntimeError(f"Profile {self.profile.id} thiếu user_data_dir")
        self.profile.user_data_dir.mkdir(parents=True, exist_ok=True)
        chrome = find_chrome(self.config.browser.chrome_executable)
        if chrome is None:
            raise RuntimeError("Không tìm thấy Google Chrome; sửa browser.chrome_executable")
        options: dict[str, Any] = {
            "user_data_dir": str(self.profile.user_data_dir),
            "executable_path": str(chrome),
            "headless": self.config.browser.headless,
            "slow_mo": self.config.browser.slow_mo_ms,
            "locale": "vi-VN",
            "accept_downloads": False,
            # Playwright disables Chromium's sandbox unless this is explicitly
            # enabled.  On Windows Chrome supports its normal sandbox, so keep
            # it on: this removes Chrome's visible --no-sandbox warning and is
            # safer for a browser that signs into game accounts.
            "chromium_sandbox": True,
        }
        args: list[str] = []
        if self.config.browser.app_mode:
            args.append(f"--app={self.config.target_url}")
        if self.config.browser.low_memory_mode:
            # Keep normal WebGL/GPU rendering, but remove background Chrome
            # services and cap spare renderer processes for each profile.
            args.extend(
                [
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-domain-reliability",
                    "--disable-extensions",
                    "--disable-features=BackForwardCache,MediaRouter,OptimizationHints,Translate",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--no-first-run",
                    "--process-per-site",
                    "--renderer-process-limit=2",
                ]
            )
        if self.config.browser.auto_resize:
            args.append(
                f"--window-size={self.config.browser.viewport_width},"
                f"{self.config.browser.viewport_height}"
            )
        if args:
            options["args"] = args
        # In headed app mode a fixed Playwright viewport does not follow a
        # native mouse resize. That leaves a white document strip below the
        # WebGL canvas. Let Chromium own the viewport and resize the native
        # renderer HWND to the requested dimensions in resize().
        if self.config.browser.auto_resize and self.config.browser.headless:
            options["viewport"] = {
                "width": self.config.browser.viewport_width,
                "height": self.config.browser.viewport_height,
            }
        else:
            options["no_viewport"] = True
        self._context = self._playwright.chromium.launch_persistent_context(**options)

    def _start_cdp(self) -> None:
        assert self._playwright is not None
        if not self.profile.cdp_url:
            raise RuntimeError(f"Profile {self.profile.id} thiếu cdp_url")
        self._browser = self._playwright.chromium.connect_over_cdp(
            self.profile.cdp_url,
            timeout=self.config.browser.startup_timeout_ms,
            slow_mo=self.config.browser.slow_mo_ms,
        )
        if not self._browser.contexts:
            raise RuntimeError("Chrome CDP không có browser context")
        self._context = self._browser.contexts[0]

    def _attach_lifecycle(self) -> None:
        self.context.on("page", self._track_page)
        self.context.on("close", lambda *_: self._mark_context_closed())
        for page in self.context.pages:
            self._track_page(page)
        if self._browser is not None:
            self._browser.on("disconnected", lambda *_: self._mark_context_closed())

    def _track_page(self, page: Page) -> None:
        key = id(page)
        if key in self._tracked_pages:
            return
        self._tracked_pages.add(key)
        page.on("close", lambda *_: self._mark_page_closed(page))
        page.on(
            "framenavigated",
            lambda frame: self._configured_frames.pop(id(frame), None),
        )

    def _mark_context_closed(self) -> None:
        if not self._closing:
            self._externally_closed = True

    def _mark_page_closed(self, page: Page) -> None:
        if page is self._runtime_page:
            self._detach_page_cdp_session()
            self._runtime_page = None
            self._direct_canvas_capture_supported = None
        if not self._closing and page is self._page:
            self._externally_closed = True

    def _ensure_page_runtime(self, page: Page) -> None:
        if page is self._runtime_page:
            return
        self._detach_page_cdp_session()
        self._runtime_page = page
        self._direct_canvas_capture_supported = None

    def _get_page_cdp_session(self, page: Page | None = None) -> Any:
        page = page or self.page
        self._ensure_page_runtime(page)
        if self._page_cdp_session is None:
            self._page_cdp_session = self.context.new_cdp_session(page)
        return self._page_cdp_session

    def _detach_page_cdp_session(self) -> None:
        session = self._page_cdp_session
        self._page_cdp_session = None
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass

    def is_alive(self) -> bool:
        if self._externally_closed or self._context is None:
            return False
        return self._page is not None and not self._page.is_closed()

    def _choose_page(self, *, create: bool = False) -> Page:
        pages = [page for page in self.context.pages if not page.is_closed()]
        for page in pages:
            if self._is_target(page.url):
                return page
        if pages:
            return pages[-1]
        if create:
            return self.context.new_page()
        raise RuntimeError("Không còn tab Chrome nào trong profile")

    def _is_target(self, url: str) -> bool:
        return is_allowed_url(url, self.config.capture.allowed_hosts)

    def _close_unused_blank_pages(self, selected: Page) -> None:
        """Close disposable startup tabs, while preserving every real page."""
        disposable = {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}
        for page in tuple(self.context.pages):
            if page is selected or page.is_closed() or page.url not in disposable:
                continue
            try:
                page.close(run_before_unload=False)
            except Exception:
                pass

    def _profile_title_script(self) -> str:
        title = json.dumps(self.profile.name, ensure_ascii=False)
        return f"""
        (() => {{
          if (window !== window.top) return;
          const desiredTitle = {title};
          const applyTitle = () => {{
            if (document.title !== desiredTitle) document.title = desiredTitle;
          }};
          if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', applyTitle, {{once: true}});
          }} else {{
            applyTitle();
          }}
          if (!window.__IK_PROFILE_TITLE_OBSERVER) {{
            window.__IK_PROFILE_TITLE_OBSERVER = new MutationObserver(applyTitle);
            window.__IK_PROFILE_TITLE_OBSERVER.observe(document.documentElement, {{
              subtree: true, childList: true, characterData: true
            }});
          }}
        }})();
        """

    def _apply_profile_title(self) -> None:
        if not self.config.browser.profile_title or self._page is None or self._page.is_closed():
            return
        try:
            self._page.evaluate(self._profile_title_script())
        except Exception:
            pass

    def goto(self, url: str | None = None) -> None:
        target = url or self.config.target_url
        self.page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=self.config.browser.startup_timeout_ms,
        )
        self._configure_interaction_frames(force=True)
        self._apply_profile_title()
        self.auto_login_if_needed()

    @staticmethod
    def _first_visible_input(frame: Frame, selectors: tuple[str, ...]) -> Any | None:
        for selector in selectors:
            try:
                locator = frame.locator(selector).first
                if locator.count() and locator.is_visible(timeout=500):
                    return locator
            except Exception:
                continue
        return None

    def auto_login_if_needed(self) -> bool:
        """Fill the game login form only when the profile has no active session."""
        try:
            from ik_chrome_auto.credential_store import WindowsCredentialStore

            credential = WindowsCredentialStore().load(self.profile.id)
        except Exception:
            return False
        if credential is None:
            return False
        for frame in self.page.frames:
            if not is_allowed_url(frame.url, self.config.capture.allowed_hosts):
                continue
            username = self._first_visible_input(frame, LOGIN_USERNAME_SELECTORS)
            password = self._first_visible_input(frame, LOGIN_PASSWORD_SELECTORS)
            if username is None or password is None:
                continue
            try:
                self._paced_login_input(username, credential.username)
                time.sleep(random.uniform(0.18, 0.36))
                self._paced_login_input(password, credential.password)
                time.sleep(random.uniform(0.30, 0.55))
                login_button = frame.get_by_role(
                    "button", name=re.compile(r"^(đăng nhập|login)$", re.IGNORECASE)
                ).first
                login_button.hover(timeout=3_000)
                time.sleep(random.uniform(0.12, 0.24))
                login_button.click(timeout=3_000)
                return True
            except Exception:
                return False
        return False

    @staticmethod
    def _paced_login_input(locator: Any, value: str) -> None:
        """Enter a credential through normal keyboard events, never log its value."""
        locator.click(timeout=3_000)
        locator.press("Control+A", timeout=3_000)
        locator.press("Backspace", timeout=3_000)
        for character in value:
            locator.press_sequentially(character, delay=random.randint(42, 88), timeout=3_000)

    def resize(self, width: int, height: int) -> None:
        width, height = validate_viewport(int(width), int(height))
        if sys.platform == "win32" and not self.config.browser.headless:
            hwnd = self.window_handle or self._bind_native_window(retries=30)
            if hwnd is None:
                raise RuntimeError(f"Cannot find the Chrome window for {self.profile.name}")
            rect = get_window_rect(hwnd)
            move_window_renderer(
                hwnd,
                rect.left,
                rect.top,
                width,
                height,
                topmost=self._topmost,
            )
            self.pump(80)
            self.config.browser.viewport_width = width
            self.config.browser.viewport_height = height
            return
        errors: list[Exception] = []
        resized = 0
        for page in self.context.pages:
            if page.is_closed():
                continue
            try:
                page.set_viewport_size({"width": width, "height": height})
                resized += 1
            except Exception as error:
                errors.append(error)
        if resized == 0 and errors:
            raise RuntimeError(f"Không resize được Chrome: {errors[-1]}")
        self.config.browser.viewport_width = width
        self.config.browser.viewport_height = height

    def read_world_position(self) -> tuple[int, int] | None:
        """Read a visible X/Y label if the portal exposes it as DOM text."""
        pattern = re.compile(
            r"\bX\s*[:\uff1a]\s*(\d{1,4})\s*Y\s*[:\uff1a]\s*(\d{1,4})\b",
            re.IGNORECASE,
        )
        for frame in reversed(self.page.frames):
            try:
                body_text = str(
                    frame.evaluate("() => (document.body && document.body.innerText) || ''")
                )
            except Exception:
                continue
            match = pattern.search(body_text)
            if match:
                return int(match.group(1)), int(match.group(2))
        return None

    def find_frame(self, url_contains: str | None = None) -> Frame:
        frames = self.page.frames
        if url_contains:
            matches = [frame for frame in frames if url_contains.lower() in frame.url.lower()]
            if not matches:
                raise RuntimeError(f"Không tìm thấy frame chứa URL: {url_contains}")
            return matches[-1]
        game_frames = [frame for frame in frames if "gtarcade.com" in frame.url.lower()]
        if game_frames:
            return game_frames[-1]
        for frame in reversed(frames):
            try:
                if frame.locator("canvas").count() > 0:
                    return frame
            except Exception:
                continue
        return self.page.main_frame

    def surface_box(self, *, target: str = "canvas", frame_url_contains: str | None = None) -> dict[str, float]:
        frame = self.find_frame(frame_url_contains)
        if target == "canvas":
            boxes: list[dict[str, float]] = []
            for index in range(frame.locator("canvas").count()):
                box = frame.locator("canvas").nth(index).bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    boxes.append(box)
            if boxes:
                return max(boxes, key=lambda item: item["width"] * item["height"])
        if frame == self.page.main_frame:
            viewport = self.page.viewport_size
            if viewport is None:
                viewport = self.page.evaluate(
                    "() => ({width: window.innerWidth, height: window.innerHeight})"
                )
            return {
                "x": 0.0,
                "y": 0.0,
                "width": float(viewport["width"]),
                "height": float(viewport["height"]),
            }
        frame_element = frame.frame_element()
        box = frame_element.bounding_box()
        if box:
            return box
        raise RuntimeError("Không xác định được vùng click của frame/canvas")

    def capture_game_surface_png(
        self, *, prefer_browser_capture: bool = False
    ) -> tuple[bytes, dict[str, float]]:
        """Capture the largest game canvas and return its viewport box.

        Headed Chrome uses the pixels already composed by Windows. Calling
        ``canvas.toDataURL`` or ``Page.captureScreenshot`` repeatedly on a
        WebGL game forces a GPU readback and visibly stalls the game window.
        Browser capture remains available for headless sessions, where there
        is no visible desktop surface to read.
        """
        page = self.page
        self._ensure_page_runtime(page)
        frame = self.find_frame()
        canvas, box = self._largest_canvas(frame)
        if (
            sys.platform == "win32"
            and not self.config.browser.headless
            and not prefer_browser_capture
        ):
            # In headed Chrome, even a single WebGL readback/screenshot can
            # make the compositor display a ghost frame. Never ask Chrome for
            # pixels in this mode; copy only what Windows already displays.
            return self._capture_visible_canvas_png(page, box), box
        png: bytes | None = None
        if self._direct_canvas_capture_supported is not False:
            try:
                data_url = canvas.evaluate("element => element.toDataURL('image/png')")
                if isinstance(data_url, str) and data_url.startswith("data:image/png;base64,"):
                    candidate = base64.b64decode(data_url.split(",", 1)[1])
                    if (
                        candidate.startswith(b"\x89PNG\r\n\x1a\n")
                        and len(candidate) > 1_000
                        and _png_has_visible_content(candidate)
                    ):
                        png = candidate
                        self._direct_canvas_capture_supported = True
                if png is None:
                    # WebGL without preserveDrawingBuffer commonly returns a
                    # valid but black PNG.  Do not repeat that GPU readback on
                    # every auto tick after the first failed probe.
                    self._direct_canvas_capture_supported = False
            except Exception:
                # Cross-origin textures can taint a canvas.  Cache the result
                # and use the non-DOM DevTools capture path from now on.
                self._direct_canvas_capture_supported = False
        if png is None:
            try:
                result = self._get_page_cdp_session(page).send(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "fromSurface": True,
                        "captureBeyondViewport": False,
                        "clip": {
                            "x": float(box["x"]),
                            "y": float(box["y"]),
                            "width": float(box["width"]),
                            "height": float(box["height"]),
                            "scale": 1,
                        },
                    },
                )
                candidate = base64.b64decode(result["data"])
                if not (
                    candidate.startswith(b"\x89PNG\r\n\x1a\n")
                    and len(candidate) > 1_000
                    and _png_has_visible_content(candidate)
                ):
                    raise RuntimeError("CDP returned an empty game screenshot")
                png = candidate
            except Exception:
                # Compatibility fallback for older Chromium/CDP builds.  It
                # may touch locator state, so it is intentionally last.
                png = bytes(
                    canvas.screenshot(
                        type="png",
                        timeout=self.config.browser.startup_timeout_ms,
                    )
                )
        return png, box

    def detect_farm_state(self) -> tuple[Any, dict[str, float], tuple[int, int]]:
        """Classify the current game canvas with the ported ADB template pack."""
        from ik_chrome_auto.farm_matcher import BrowserCanvasMatcher

        # Do not force a CDP screenshot for every farm tick in a headed
        # profile.  WebGL readback synchronises Chrome's compositor and makes
        # the game visibly jerk.  The visible Windows capture path is both
        # stable for the player and sufficient for template matching.  A
        # headless profile necessarily keeps the browser capture path.
        png, surface = self.capture_game_surface_png(
            prefer_browser_capture=self.config.browser.headless
        )
        # The worker can retain this exact input image on a failed preflight.
        # It is intentionally kept only in memory until a diagnostic is needed.
        self._last_farm_capture_png = png
        if self._farm_matcher is None:
            self._farm_matcher = BrowserCanvasMatcher()
        result = self._farm_matcher.detect(png)
        return result, surface, self._png_dimensions(png)

    def last_farm_capture_png(self) -> bytes | None:
        """Return the most recent farm canvas capture for local diagnostics."""
        return self._last_farm_capture_png

    @staticmethod
    def _png_dimensions(png: bytes) -> tuple[int, int]:
        image = decode_png(png)
        return image.width, image.height

    def tap_farm_template(self, bounds: tuple[int, int, int, int], image_size: tuple[int, int]) -> None:
        """Send one CDP touch at freshly matched screenshot-relative bounds."""
        left, top, width, height = bounds
        image_width, image_height = image_size
        if width <= 0 or height <= 0 or image_width <= 0 or image_height <= 0:
            raise ValueError("Farm template bounds không hợp lệ")
        frame = self.find_frame()
        _canvas, surface = self._largest_canvas(frame)
        x = float(surface["x"]) + float(surface["width"]) * (left + width / 2) / image_width
        y = float(surface["y"]) + float(surface["height"]) * (top + height / 2) / image_height
        point = {"x": x, "y": y, "radiusX": 2, "radiusY": 2, "force": 1, "id": 1}
        cdp = self._get_page_cdp_session(self.page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def _capture_visible_canvas_png(
        self,
        page: Page,
        box: dict[str, float],
    ) -> bytes:
        hwnd = self.window_handle or self._bind_native_window(retries=10)
        if hwnd is None:
            raise RuntimeError(f"Không tìm thấy cửa sổ Windows của {self.profile.name}")
        renderer = get_renderer_rect(hwnd)
        viewport = page.viewport_size or page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        viewport_width = max(1.0, float(viewport["width"]))
        viewport_height = max(1.0, float(viewport["height"]))
        scale_x = renderer.width / viewport_width
        scale_y = renderer.height / viewport_height
        left = renderer.left + round(float(box["x"]) * scale_x)
        top = renderer.top + round(float(box["y"]) * scale_y)
        right = renderer.left + round((float(box["x"]) + float(box["width"])) * scale_x)
        bottom = renderer.top + round((float(box["y"]) + float(box["height"])) * scale_y)
        region = WindowRect(left, top, right, bottom)
        if not is_region_visible_for_window(hwnd, region):
            raise RuntimeError(
                "Cửa sổ game đang bị thu nhỏ hoặc bị cửa sổ khác che; "
                "hãy bấm Sắp xếp cửa sổ trước khi chạy Auto 2048"
            )
        png = capture_screen_region_png(region)
        if not _png_has_visible_content(png):
            raise RuntimeError("Ảnh màn hình game trống; hãy đưa cửa sổ profile ra màn hình")
        return png

    def swipe_game_surface(
        self,
        direction: str,
        grid_box: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> None:
        """Swipe inside a detected board using screenshot-relative coordinates."""
        if direction not in {"left", "right", "up", "down"}:
            raise ValueError(f"Hướng vuốt không hợp lệ: {direction}")
        frame = self.find_frame()
        _canvas, surface = self._largest_canvas(frame)
        image_width, image_height = image_size
        left, top, right, bottom = grid_box
        board_left = surface["x"] + surface["width"] * left / image_width
        board_right = surface["x"] + surface["width"] * right / image_width
        board_top = surface["y"] + surface["height"] * top / image_height
        board_bottom = surface["y"] + surface["height"] * bottom / image_height
        centre_x = (board_left + board_right) / 2.0
        centre_y = (board_top + board_bottom) / 2.0
        inset_x = (board_right - board_left) * 0.22
        inset_y = (board_bottom - board_top) * 0.22
        points = {
            "left": ((board_right - inset_x, centre_y), (board_left + inset_x, centre_y)),
            "right": ((board_left + inset_x, centre_y), (board_right - inset_x, centre_y)),
            "up": ((centre_x, board_bottom - inset_y), (centre_x, board_top + inset_y)),
            "down": ((centre_x, board_top + inset_y), (centre_x, board_bottom - inset_y)),
        }
        start, end = points[direction]
        # The mini game is implemented for mobile touch input.  A Playwright
        # mouse drag can focus/flash the Chrome window without moving the
        # board.  CDP touch events work without moving the real cursor and are
        # handled by the same swipe listener as a phone.
        cdp = self._get_page_cdp_session(self.page)

        def touch_point(x: float, y: float) -> dict[str, float | int]:
            return {
                "x": x,
                "y": y,
                "radiusX": 2,
                "radiusY": 2,
                "force": 1,
                "id": 1,
            }

        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [touch_point(*start)]},
        )
        for step in range(1, 10):
            ratio = step / 9.0
            x = start[0] + (end[0] - start[0]) * ratio
            y = start[1] + (end[1] - start[1]) * ratio
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchMove", "touchPoints": [touch_point(x, y)]},
            )
        cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchEnd", "touchPoints": []},
            )

    def _largest_canvas(self, frame: Frame) -> tuple[Any, dict[str, float]]:
        canvases = frame.locator("canvas")
        candidates: list[tuple[Any, dict[str, float]]] = []
        for index in range(canvases.count()):
            locator = canvases.nth(index)
            box = locator.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                candidates.append((locator, box))
        if not candidates:
            raise RuntimeError("Không tìm thấy canvas game đang hiển thị")
        return max(candidates, key=lambda item: item[1]["width"] * item[1]["height"])

    def scroll_game_surface(self, delta_y: float) -> None:
        """Send a wheel event at the canvas centre without moving the real cursor."""
        frame = self.find_frame()
        _canvas, surface = self._largest_canvas(frame)
        x = float(surface["x"]) + float(surface["width"]) / 2.0
        y = float(surface["y"]) + float(surface["height"]) / 2.0
        self._get_page_cdp_session(self.page).send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": 0,
                "deltaY": float(delta_y),
                "modifiers": 0,
                "pointerType": "mouse",
            },
        )

    def press_escape(self) -> None:
        """Dismiss the current WebGL popup without focusing the real window."""
        cdp = self._get_page_cdp_session(self.page)
        for event_type in ("keyDown", "keyUp"):
            cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": event_type,
                    "key": "Escape",
                    "code": "Escape",
                    "windowsVirtualKeyCode": 27,
                    "nativeVirtualKeyCode": 27,
                },
            )

    def tap_game_surface_ratio(self, x_ratio: float, y_ratio: float) -> None:
        """Tap a canvas-relative point without moving the physical mouse."""
        if not 0.0 <= x_ratio <= 1.0 or not 0.0 <= y_ratio <= 1.0:
            raise ValueError("Canvas ratios must be between 0 and 1")
        frame = self.find_frame()
        _canvas, surface = self._largest_canvas(frame)
        x = float(surface["x"]) + float(surface["width"]) * float(x_ratio)
        y = float(surface["y"]) + float(surface["height"]) * float(y_ratio)
        point = {
            "x": x,
            "y": y,
            "radiusX": 2,
            "radiusY": 2,
            "force": 1,
            "id": 1,
        }
        cdp = self._get_page_cdp_session(self.page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def set_sync_source(self, enabled: bool) -> None:
        self._sync_source = bool(enabled)
        self._configure_interaction_frames(force=True)

    def set_inspector(self, enabled: bool) -> None:
        self._inspector_enabled = bool(enabled)
        self._configure_interaction_frames(force=True)

    def set_drag_item_visible(self, visible: bool) -> None:
        self._drag_item_visible = bool(visible)
        self._configure_interaction_frames(force=True)

    def set_scrollbars_visible(self, visible: bool) -> None:
        """Show or hide page scrollbars without changing scroll behaviour."""
        self._scrollbars_visible = bool(visible)
        self._configure_interaction_frames(force=True)

    @property
    def window_handle(self) -> int | None:
        if is_window(self._window_handle):
            return self._window_handle
        self._window_handle = find_chrome_window(self.profile.name)
        if self._window_handle is not None:
            set_taskbar_group(self._window_handle)
        return self._window_handle

    def _bind_native_window(self, retries: int = 30) -> int | None:
        if self.config.browser.headless:
            return None
        for _attempt in range(max(1, retries)):
            hwnd = find_chrome_window(self.profile.name)
            if hwnd is not None:
                self._window_handle = hwnd
                set_taskbar_group(hwnd)
                return hwnd
            time.sleep(0.1)
        return None

    def move_window(
        self,
        x: int,
        y: int,
        *,
        topmost: bool,
    ) -> None:
        hwnd = self.window_handle or self._bind_native_window(retries=10)
        if hwnd is None:
            raise RuntimeError(f"Không tìm thấy cửa sổ Windows của {self.profile.name}")
        self._topmost = bool(topmost)
        move_window_position(hwnd, int(x), int(y), topmost=topmost)

    def set_topmost(self, enabled: bool) -> None:
        hwnd = self.window_handle or self._bind_native_window(retries=10)
        if hwnd is None:
            raise RuntimeError(f"Không tìm thấy cửa sổ Windows của {self.profile.name}")
        self._topmost = bool(enabled)
        set_native_topmost(hwnd, self._topmost)

    def poll_sync_events(self) -> list[dict[str, Any]]:
        self._configure_interaction_frames()
        events: list[dict[str, Any]] = []
        if not self._sync_source:
            return events
        for frame in self.page.frames:
            try:
                rows = frame.evaluate("() => window.__IK_SYNC_EVENTS?.splice(0) || []")
            except Exception:
                continue
            for row in rows:
                row["frame_url"] = frame.url
                row["frame_url_safe"] = redact_url(frame.url)
                events.append(row)
        return events

    def poll_coordinate_events(self) -> list[dict[str, Any]]:
        self._configure_interaction_frames()
        events: list[dict[str, Any]] = []
        if not self._inspector_enabled:
            return events
        for frame in self.page.frames:
            try:
                rows = frame.evaluate("() => window.__IK_COORDINATE_EVENTS?.splice(0) || []")
            except Exception:
                continue
            for row in rows:
                row["frame_url"] = frame.url
                row["frame_url_safe"] = redact_url(frame.url)
                events.append(row)
        return events

    def apply_synced_input(self, event: dict[str, Any]) -> None:
        frame = self._frame_for_input(event)
        canvas = event.get("canvas")
        if isinstance(canvas, dict):
            box = self._canvas_box(frame, int(canvas.get("index", 0)))
        else:
            box = self._frame_box(frame)
        x, y = calculate_target_point(event, box)
        event_type = str(event.get("type", ""))
        button_number = int(event.get("pointer", {}).get("button", 0))
        button = {0: "left", 1: "middle", 2: "right"}.get(button_number, "left")
        if event_type == "pointerdown":
            self.page.mouse.move(x, y)
            self.page.mouse.down(button=button)
        elif event_type == "pointermove":
            self.page.mouse.move(x, y)
        elif event_type == "pointerup":
            self.page.mouse.move(x, y)
            self.page.mouse.up(button=button)
        elif event_type == "wheel":
            wheel = event.get("wheel", {})
            self.page.mouse.move(x, y)
            self.page.mouse.wheel(
                float(wheel.get("delta_x", 0.0)),
                float(wheel.get("delta_y", 0.0)),
            )

    def _configure_interaction_frames(self, *, force: bool = False) -> None:
        if self._page is None or self._page.is_closed():
            return
        active_keys: set[int] = set()
        for frame in self.page.frames:
            key = id(frame)
            active_keys.add(key)
            signature = (
                f"{frame.url}|{self._sync_source}|{self._inspector_enabled}|"
                f"{self._drag_item_visible}|{self._scrollbars_visible}"
            )
            if not force and self._configured_frames.get(key) == signature:
                continue
            try:
                frame.evaluate(INTERACTION_PROBE)
                frame.evaluate(
                    "([syncSource, inspectEnabled]) => "
                    "window.__IK_SET_INTERACTION_MODES?.(syncSource, inspectEnabled)",
                    [self._sync_source, self._inspector_enabled],
                )
                frame.evaluate(
                    "visible => window.__IK_SET_DRAG_ITEM_VISIBLE?.(visible)",
                    self._drag_item_visible,
                )
                frame.evaluate(
                    """visible => {
                        const styleId = '__ik_auto_scrollbars';
                        let style = document.getElementById(styleId);
                        if (!style) {
                            style = document.createElement('style');
                            style.id = styleId;
                            (document.head || document.documentElement).appendChild(style);
                        }
                        style.textContent = visible ? '' : `
                            html, body, * { scrollbar-width: none !important; -ms-overflow-style: none !important; }
                            *::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
                        `;
                    }""",
                    self._scrollbars_visible,
                )
                self._configured_frames[key] = signature
            except Exception:
                continue
        self._configured_frames = {
            key: value for key, value in self._configured_frames.items() if key in active_keys
        }

    def _frame_for_input(self, event: dict[str, Any]) -> Frame:
        source_url = str(event.get("frame_url", ""))
        source_host = urlsplit(source_url).hostname or ""
        if source_host:
            matches = [
                frame
                for frame in self.page.frames
                if (urlsplit(frame.url).hostname or "").lower() == source_host.lower()
            ]
            if matches:
                return matches[-1]
        return self.find_frame()

    def _canvas_box(self, frame: Frame, index: int) -> dict[str, float]:
        canvases = frame.locator("canvas")
        if 0 <= index < canvases.count():
            box = canvases.nth(index).bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                return box
        boxes: list[dict[str, float]] = []
        for candidate_index in range(canvases.count()):
            box = canvases.nth(candidate_index).bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                boxes.append(box)
        if boxes:
            return max(boxes, key=lambda item: item["width"] * item["height"])
        return self._frame_box(frame)

    def _frame_box(self, frame: Frame) -> dict[str, float]:
        if frame == self.page.main_frame:
            viewport = self.page.viewport_size or self.page.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
            return {
                "x": 0.0,
                "y": 0.0,
                "width": float(viewport["width"]),
                "height": float(viewport["height"]),
            }
        box = frame.frame_element().bounding_box()
        if box:
            return box
        raise RuntimeError("Không xác định được vị trí frame đích để sync chuột")

    def pump(self, milliseconds: int = 50) -> None:
        if self._page is not None and not self._page.is_closed():
            self._page.wait_for_timeout(milliseconds)
            self._configure_interaction_frames()

    def close(self) -> None:
        self._closing = True
        try:
            self._detach_page_cdp_session()
            if self._managed and self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._window_handle = None
            self._runtime_page = None
            self._direct_canvas_capture_supported = None
            self._configured_frames.clear()
            self._tracked_pages.clear()
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._closing = False
