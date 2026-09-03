from __future__ import annotations

import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from ik_chrome_auto.interaction import (
    INTERACTION_PROBE,
    calculate_target_point,
    validate_viewport,
)
from ik_chrome_auto.image_utils import RGBImage, decode_png
from ik_chrome_auto.config import is_allowed_url
from ik_chrome_auto.chrome_preferences import suppress_browser_prompts
from ik_chrome_auto.models import AppConfig, ProfileConfig, ProfileMode
from ik_chrome_auto.reader import GameDataReader, redact_url
from ik_chrome_auto.windows import (
    WindowRect,
    capture_screen_region_png,
    find_chrome_window,
    find_chrome_window_for_process,
    find_tcp_listener_process,
    get_window_rect,
    get_renderer_rect,
    is_window,
    is_window_minimized,
    move_window_position,
    move_window_renderer,
    set_window_minimized,
    set_taskbar_group,
    set_topmost as set_native_topmost,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright


KNOWN_CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

_PROFILE_CDP_PORT_MIN = 21000
_PROFILE_CDP_PORT_SPAN = 20_000


def _profile_cdp_port(profile_id: str) -> int:
    """Return a stable local DevTools port without persisting runtime state."""
    # ``hash()`` is intentionally randomised between Python processes.  A
    # stable digest lets a newly updated tool reconnect to Chrome launched by
    # the previous version.
    import hashlib

    digest = hashlib.sha256(profile_id.encode("utf-8")).digest()
    return _PROFILE_CDP_PORT_MIN + int.from_bytes(digest[:4], "big") % _PROFILE_CDP_PORT_SPAN


def _cdp_endpoint_is_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint.rstrip('/')}/json/version", timeout=0.5) as response:
            return response.status == 200
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class AutomationRendererLayout:
    """Native Chrome geometry saved before a temporary automation resize."""

    outer: WindowRect
    renderer: WindowRect
    resized: bool

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


def _low_gpu_init_script(fps_limit: int) -> str:
    """Reduce WebGL cost while preserving real-time page/network execution."""
    interval_ms = 1000.0 / min(60, max(10, int(fps_limit)))
    return f"""
    (() => {{
      window.__IK_RENDER_INTERVAL_MS = {interval_ms:.4f};
      if (window.__IK_LOW_GPU_MODE) return;
      window.__IK_LOW_GPU_MODE = true;
      const originalGetContext = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function(type, attributes) {{
        const kind = String(type || '').toLowerCase();
        if (kind === 'webgl' || kind === 'webgl2' || kind === 'experimental-webgl') {{
          attributes = Object.assign({{}}, attributes || {{}}, {{
            antialias: false,
            preserveDrawingBuffer: false,
            powerPreference: 'low-power'
          }});
        }}
        return originalGetContext.call(this, type, attributes);
      }};

      const nativeRequest = window.requestAnimationFrame.bind(window);
      const nativeCancel = window.cancelAnimationFrame.bind(window);
      let sequence = 1;
      const pending = new Map();
      window.requestAnimationFrame = callback => {{
        const token = sequence++;
        const state = {{ nativeId: 0, cancelled: false, startedAt: performance.now() }};
        const tick = timestamp => {{
          if (state.cancelled) return;
          const interval = Number(window.__IK_RENDER_INTERVAL_MS) || {interval_ms:.4f};
          if (timestamp - state.startedAt + 0.1 >= interval) {{
            pending.delete(token);
            callback(timestamp);
            return;
          }}
          state.nativeId = nativeRequest(tick);
        }};
        state.nativeId = nativeRequest(tick);
        pending.set(token, state);
        return token;
      }};
      window.cancelAnimationFrame = token => {{
        const state = pending.get(token);
        if (!state) return nativeCancel(token);
        state.cancelled = true;
        nativeCancel(state.nativeId);
        pending.delete(token);
      }};
    }})();
    """


def _game_frame_fit_init_script() -> str:
    """Return persistent CSS that removes host and game-frame gutters."""
    return r"""
    (() => {
      const iframeSelector =
        'iframe.iframe[src*="gtarcade.com"], iframe[src*="union.gtarcade.com/channel/"]';
      const zeroDocumentGutter = () => {
        let isGtarcadeDocument = false;
        try {
          isGtarcadeDocument =
            location.hostname === 'gtarcade.com' ||
            location.hostname.endsWith('.gtarcade.com');
        } catch (_) {}
        if (!isGtarcadeDocument && !document.querySelector(iframeSelector)) return;
        for (const node of [document.documentElement, document.body]) {
          if (!node) continue;
          node.style.setProperty('margin', '0', 'important');
          node.style.setProperty('padding', '0', 'important');
          node.style.setProperty('width', '100%', 'important');
          node.style.setProperty('height', '100%', 'important');
          node.style.setProperty('overflow', 'hidden', 'important');
        }
      };
      const install = () => {
        const root = document.head || document.documentElement;
        if (!root) return;
        const styleId = '__ik_auto_game_frame_fit';
        let style = document.getElementById(styleId);
        if (!style) {
          style = document.createElement('style');
          style.id = styleId;
          root.appendChild(style);
        }
        style.textContent = `
          iframe.iframe[src*="gtarcade.com"],
          iframe[src*="union.gtarcade.com/channel/"] {
            position: fixed !important;
            inset: 0 !important;
            display: block !important;
            box-sizing: border-box !important;
            width: 100vw !important;
            height: 100vh !important;
            max-width: none !important;
            max-height: none !important;
            margin: 0 !important;
            padding: 0 !important;
            border: 0 !important;
          }
        `;
        zeroDocumentGutter();
      };
      install();
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', install, { once: true });
      }
      if (!window.__IK_AUTO_GAME_FRAME_FIT_OBSERVER && document.documentElement) {
        window.__IK_AUTO_GAME_FRAME_FIT_OBSERVER = new MutationObserver(
          zeroDocumentGutter
        );
        window.__IK_AUTO_GAME_FRAME_FIT_OBSERVER.observe(
          document.documentElement,
          { childList: true, subtree: true }
        );
      }
    })();
    """


GAME_FRAME_FIT_SCRIPT = _game_frame_fit_init_script()


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
        # Managed profiles are launched as independent Chrome processes and
        # controlled through CDP.  Unlike Playwright's persistent-context
        # launch, disconnecting this tool must never close their windows.
        self._owns_browser_process = False
        self._sync_source = False
        self._inspector_enabled = False
        self._drag_item_visible = False
        self._scrollbars_visible = False
        self._window_handle: int | None = None
        self._managed_browser_pid: int | None = None
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
        if self.config.browser.low_gpu_mode:
            low_gpu_script = _low_gpu_init_script(self.config.browser.render_fps_limit)
            self.context.add_init_script(low_gpu_script)
            for existing_page in self.context.pages:
                for frame in existing_page.frames:
                    try:
                        frame.evaluate(low_gpu_script)
                    except Exception:
                        continue
        self.context.add_init_script(INTERACTION_PROBE)
        # Install before any later portal navigation. Inline width/height on
        # the GTArcade iframe is intentionally overridden with !important so
        # the host's default 8 px body margin cannot leave a black frame.
        self.context.add_init_script(GAME_FRAME_FIT_SCRIPT)
        for existing_page in self.context.pages:
            for frame in existing_page.frames:
                try:
                    frame.evaluate(GAME_FRAME_FIT_SCRIPT)
                except Exception:
                    continue
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
        suppress_browser_prompts(self.profile.user_data_dir)
        chrome = find_chrome(self.config.browser.chrome_executable)
        if chrome is None:
            raise RuntimeError("Không tìm thấy Google Chrome; sửa browser.chrome_executable")
        port = _profile_cdp_port(self.profile.id)
        endpoint = f"http://127.0.0.1:{port}"
        args: list[str] = [
            str(chrome),
            f"--user-data-dir={self.profile.user_data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--disable-notifications",
            "--no-default-browser-check",
            "--no-first-run",
            "--lang=vi-VN",
        ]
        if self.config.browser.headless:
            args.append("--headless=new")
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
                    "--process-per-site",
                    "--renderer-process-limit=2",
                ]
            )
        if self.config.browser.low_gpu_mode:
            # Avoid rendering a larger backing surface on high-DPI monitors.
            # Keep hardware WebGL enabled; software rendering would move the
            # bottleneck to CPU and destabilise large profile counts.
            args.append("--force-device-scale-factor=1")
        if self.config.browser.auto_resize:
            args.append(
                f"--window-size={self.config.browser.viewport_width},"
                f"{self.config.browser.viewport_height}"
            )
        if not _cdp_endpoint_is_ready(endpoint):
            # Detach Chrome from the tool process.  The browser survives an
            # application close/update and is reused by the next launch.
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
            try:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    close_fds=True,
                )
                self._managed_browser_pid = int(process.pid)
            except OSError as error:
                raise RuntimeError(f"Không thể mở Chrome cho profile {self.profile.name}: {error}") from error

        deadline = time.monotonic() + self.config.browser.startup_timeout_ms / 1000
        while not _cdp_endpoint_is_ready(endpoint):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Chrome của profile {self.profile.name} không mở cổng điều khiển. "
                    "Hãy đóng riêng cửa sổ Chrome của profile này rồi thử lại."
                )
            time.sleep(0.2)
        # The CDP listener belongs to the correct profile's Chrome process.
        # Resolve it even when reconnecting to a window retained across an
        # IK Auto update, where the current page title may no longer match.
        listener_pid = find_tcp_listener_process(port)
        if listener_pid is not None:
            self._managed_browser_pid = listener_pid
        self._connect_cdp(endpoint)

    def _start_cdp(self) -> None:
        assert self._playwright is not None
        if not self.profile.cdp_url:
            raise RuntimeError(f"Profile {self.profile.id} thiếu cdp_url")
        self._connect_cdp(self.profile.cdp_url)

    def _connect_cdp(self, endpoint: str) -> None:
        assert self._playwright is not None
        self._browser = self._playwright.chromium.connect_over_cdp(
            endpoint,
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
            # A slower workstation can expose CDP before its top-level Chrome
            # window becomes visible. Give the native HWND up to ten seconds.
            hwnd = self.window_handle or self._bind_native_window(retries=100)
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

    def ensure_minimum_game_renderer(self, width: int, height: int) -> bool:
        """Enlarge only this live Chrome renderer when Farm needs detail.

        This deliberately does not update the shared configured viewport.
        The window arranger is allowed to keep compact monitoring tiles, but
        template-driven Farm cannot safely identify a team roster or resource
        controls after a renderer has been shrunk below its visual minimum.
        """
        width, height = validate_viewport(int(width), int(height))
        if sys.platform != "win32" or self.config.browser.headless:
            return False
        hwnd = self.window_handle or self._bind_native_window(retries=10)
        if hwnd is None:
            raise RuntimeError(f"Không tìm thấy cửa sổ Chrome của {self.profile.name}")
        current = get_renderer_rect(hwnd)
        if current.width >= width and current.height >= height:
            return False
        outer = get_window_rect(hwnd)
        move_window_renderer(
            hwnd,
            outer.left,
            outer.top,
            width,
            height,
            topmost=self._topmost,
        )
        self.pump(120)
        return True

    def begin_automation_renderer(
        self, width: int, height: int
    ) -> AutomationRendererLayout | None:
        """Temporarily give one headed profile a high-detail game renderer.

        The dashboard may show compact window tiles, but enlarging a captured
        366×168 canvas cannot recreate the pixels needed for reliable Farm or
        mailbox recognition.  This records the exact grid geometry, resizes
        the live renderer to the requested automation size, and leaves it to
        the caller to restore the saved layout through
        :meth:`restore_automation_renderer`.

        Headless and non-Windows sessions already use their browser capture
        path and do not own a native renderer window, so they return ``None``.
        """
        width, height = validate_viewport(int(width), int(height))
        if sys.platform != "win32" or self.config.browser.headless:
            return None
        hwnd = self.window_handle or self._bind_native_window(retries=10)
        if hwnd is None:
            raise RuntimeError(f"Không tìm thấy cửa sổ Chrome của {self.profile.name}")
        if is_window_minimized(hwnd):
            set_window_minimized(hwnd, False)
            self.pump(80)
        outer = get_window_rect(hwnd)
        renderer = get_renderer_rect(hwnd)
        resized = renderer.width < width or renderer.height < height
        layout = AutomationRendererLayout(outer=outer, renderer=renderer, resized=resized)
        if not resized:
            return layout
        move_window_renderer(
            hwnd,
            outer.left,
            outer.top,
            width,
            height,
            topmost=self._topmost,
        )
        # Chromium needs a short settle period before its WebGL canvas reflects
        # the native renderer dimensions in a DevTools screenshot.
        self.pump(220)
        return layout

    def restore_automation_renderer(self, layout: AutomationRendererLayout | None) -> None:
        """Restore the compact grid geometry saved for a temporary renderer."""
        if layout is None or not layout.resized:
            return
        if sys.platform != "win32" or self.config.browser.headless:
            return
        hwnd = self.window_handle or self._bind_native_window(retries=3)
        if hwnd is None:
            return
        # Never persist iconic geometry (-32000, -32000 on Windows) or leave
        # an active Farm profile represented only by its taskbar button.
        if is_window_minimized(hwnd):
            set_window_minimized(hwnd, False)
            self.pump(80)
        move_window_renderer(
            hwnd,
            layout.outer.left,
            layout.outer.top,
            layout.renderer.width,
            layout.renderer.height,
            topmost=self._topmost,
        )
        self.pump(120)

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

    def _frame_roots(self, *, max_nested_depth: int = 3) -> list[Any]:
        """Return Playwright roots that may contain the live game canvas.

        GTArcade normally appears as a regular ``Frame`` in ``page.frames``.
        Some Chrome builds expose the login iframe there but keep the nested
        WebGL iframe only as a ``FrameLocator`` (not as another Frame entry).
        Walking iframe locators as well keeps retained/local Chrome profiles
        capturable without reloading their game session.
        """
        roots: list[Any] = list(reversed(self.page.frames))
        frontier: list[Any] = list(roots)
        for _depth in range(max_nested_depth):
            nested: list[Any] = []
            for root in frontier:
                try:
                    iframes = root.locator("iframe")
                    count = min(iframes.count(), 16)
                except Exception:
                    continue
                for index in range(count):
                    try:
                        child = iframes.nth(index).content_frame
                    except Exception:
                        continue
                    if child is not None:
                        roots.append(child)
                        nested.append(child)
            if not nested:
                break
            frontier = nested
        return roots

    @staticmethod
    def _visible_canvas(root: Any) -> tuple[Any, dict[str, float]] | None:
        try:
            canvases = root.locator("canvas")
            candidates: list[tuple[Any, dict[str, float]]] = []
            for index in range(canvases.count()):
                locator = canvases.nth(index)
                box = locator.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    candidates.append((locator, box))
        except Exception:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[1]["width"] * item[1]["height"])

    def find_frame(self, url_contains: str | None = None) -> Any:
        frames = self.page.frames
        if url_contains:
            matches = [frame for frame in frames if url_contains.lower() in frame.url.lower()]
            if not matches:
                raise RuntimeError(f"Không tìm thấy frame chứa URL: {url_contains}")
            return matches[-1]

        visible_roots: list[tuple[Any, float]] = []
        for root in self._frame_roots():
            candidate = self._visible_canvas(root)
            if candidate is not None:
                _canvas, box = candidate
                visible_roots.append((root, box["width"] * box["height"]))
        if visible_roots:
            return max(visible_roots, key=lambda item: item[1])[0]

        game_frames = [frame for frame in frames if "gtarcade.com" in frame.url.lower()]
        if game_frames:
            return game_frames[-1]
        return self.page.main_frame

    def surface_box(self, *, target: str = "canvas", frame_url_contains: str | None = None) -> dict[str, float]:
        frame = self.find_frame(frame_url_contains)
        if target == "canvas":
            candidate = self._visible_canvas(frame)
            if candidate is not None:
                return candidate[1]
            if frame_url_contains is None:
                # A cross-process WebGL iframe can remain visible in Chrome's
                # compositor without being attached to Playwright's Frame
                # tree. The portal occupies the complete viewport, so this is
                # the safe coordinate surface for a freshly verified image.
                return self._viewport_surface_box()
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

    def _viewport_surface_box(self) -> dict[str, float]:
        page = self.page
        viewport = page.viewport_size or page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        box = {
            "x": 0.0,
            "y": 0.0,
            "width": max(1.0, float(viewport["width"])),
            "height": max(1.0, float(viewport["height"])),
        }
        try:
            frame_fit_active = bool(
                page.evaluate(
                    """() => Boolean(
                        document.getElementById('__ik_auto_game_frame_fit') &&
                        document.querySelector(
                            'iframe.iframe[src*="gtarcade.com"], iframe[src*="union.gtarcade.com/channel/"]'
                        )
                    )"""
                )
            )
        except Exception:
            frame_fit_active = False
        if frame_fit_active:
            # The target iframe is fixed to the viewport; there is no gutter
            # to infer even when 1296x736 happens to become 16:9 after an
            # eight-pixel crop.
            return box
        # Some retained GTArcade profiles keep their WebGL iframe in the
        # compositor after Chromium has detached it from Playwright's frame
        # tree.  The host page then exposes only its default 8 px body gutter:
        # a 1280x720 game is captured as 1296x736.  Treating that whole image
        # as the game scales every template and click against the border,
        # leaving a clearly visible City at ``Unknown`` forever.  Infer the
        # centred game rectangle only when removing the conventional gutter
        # makes the surface materially closer to the game's authored 16:9
        # aspect ratio. Full-viewport and compact non-16:9 portals are left
        # untouched.
        gutter = 8.0
        inner_width = box["width"] - 2 * gutter
        inner_height = box["height"] - 2 * gutter
        if inner_width > 0 and inner_height > 0:
            reference_aspect = 16 / 9
            outer_error = abs(box["width"] / box["height"] - reference_aspect)
            inner_error = abs(inner_width / inner_height - reference_aspect)
            if inner_error + 0.002 < outer_error:
                return {
                    "x": gutter,
                    "y": gutter,
                    "width": inner_width,
                    "height": inner_height,
                }
        return box

    def _canvas_or_viewport(self) -> tuple[Any | None, dict[str, float]]:
        frame = self.find_frame()
        try:
            return self._largest_canvas(frame)
        except RuntimeError:
            return None, self._viewport_surface_box()

    def capture_game_surface_png(
        self, *, prefer_browser_capture: bool = False
    ) -> tuple[bytes, dict[str, float]]:
        """Capture the largest game canvas and return its viewport box.

        The capture is taken from this profile's Chrome renderer, never from
        desktop pixels. This keeps Farm correct when another application or
        profile window overlaps the visible Chrome window. Headed WebGL uses
        a clipped ``Page.captureScreenshot`` first; it is a little more
        expensive than a GDI copy, but it cannot accidentally capture another
        window.
        """
        page = self.page
        self._ensure_page_runtime(page)
        canvas, box = self._canvas_or_viewport()
        if sys.platform == "win32" and not self.config.browser.headless:
            # ``toDataURL`` can synchronously read a WebGL buffer and cause a
            # visible frame hitch. Go straight to CDP's clipped renderer
            # capture in headed mode; unlike GDI it is also independent of
            # z-order and desktop occlusion.
            self._direct_canvas_capture_supported = False
        png: bytes | None = None
        if canvas is not None and self._direct_canvas_capture_supported is not False:
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
                params: dict[str, Any] = {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": False,
                }
                clips_composited_gutter = False
                if canvas is None:
                    viewport = page.viewport_size or page.evaluate(
                        "() => ({width: window.innerWidth, height: window.innerHeight})"
                    )
                    clips_composited_gutter = (
                        float(box["x"]) != 0.0
                        or float(box["y"]) != 0.0
                        or abs(float(box["width"]) - float(viewport["width"])) > 0.5
                        or abs(float(box["height"]) - float(viewport["height"])) > 0.5
                    )
                if canvas is not None or clips_composited_gutter:
                    params["clip"] = {
                            "x": float(box["x"]),
                            "y": float(box["y"]),
                            "width": float(box["width"]),
                            "height": float(box["height"]),
                            "scale": 1,
                    }
                result = self._get_page_cdp_session(page).send(
                    "Page.captureScreenshot",
                    params,
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
                if canvas is not None:
                    png = bytes(
                        canvas.screenshot(
                            type="png",
                            timeout=self.config.browser.startup_timeout_ms,
                        )
                    )
                elif sys.platform == "win32" and not self.config.browser.headless:
                    png = self._capture_visible_canvas_png(page, box)
                else:
                    raise RuntimeError("Chrome returned an empty game screenshot")
                if not _png_has_visible_content(png):
                    raise RuntimeError("Chrome returned a black game screenshot")
        return png, box

    def capture_game_region_png(
        self,
        region: tuple[float, float, float, float],
        *,
        scale: float = 0.75,
    ) -> tuple[bytes, tuple[int, int], float]:
        """Capture one normalized canvas ROI directly from this renderer.

        Monitoring uses this instead of a full canvas screenshot. A cropped,
        downscaled DevTools capture cuts encoded pixels substantially and lets
        a few profiles be checked concurrently without reading desktop pixels.
        """
        page = self.page
        self._ensure_page_runtime(page)
        _canvas, box = self._canvas_or_viewport()
        left, top, right, bottom = region
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            raise ValueError("Vùng chụp giám sát không hợp lệ")
        scale = max(0.35, min(1.0, float(scale)))
        clip = {
            "x": round(float(box["x"]) + float(box["width"]) * left, 4),
            "y": round(float(box["y"]) + float(box["height"]) * top, 4),
            "width": round(float(box["width"]) * (right - left), 4),
            "height": round(float(box["height"]) * (bottom - top), 4),
            "scale": scale,
        }
        result = self._get_page_cdp_session(page).send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": clip,
            },
        )
        png = base64.b64decode(result["data"])
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) <= 200:
            raise RuntimeError("CDP returned an empty monitoring screenshot")
        return (
            png,
            (round(float(box["width"])), round(float(box["height"]))),
            scale,
        )

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
        x, y = self._farm_template_center(bounds, image_size)
        point = {"x": x, "y": y, "radiusX": 2, "radiusY": 2, "force": 1, "id": 1}
        cdp = self._get_page_cdp_session(self.page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def read_focused_numeric_farm_input(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> int | None:
        """Focus a verified coordinate field and return its numeric DOM value.

        This intentionally refuses canvas-only focus: callers must not alter
        World Map coordinates unless the browser exposes a readable input, so
        the original X/Y values remain available for rollback.
        """
        self.tap_farm_template(bounds, image_size)
        cdp = self._get_page_cdp_session(self.page)
        result = cdp.send(
            "Runtime.evaluate",
            {
                "expression": "(() => { const e = document.activeElement; return e && 'value' in e ? String(e.value) : null; })()",
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value") if isinstance(result, dict) else None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def replace_focused_farm_input(self, value: int) -> bool:
        """Replace the currently verified browser input and confirm its value."""
        if value < 0:
            return False
        cdp = self._get_page_cdp_session(self.page)
        for event in (
            {"type": "keyDown", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17},
            {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
            {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
            {"type": "keyUp", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17},
            {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
            {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
        ):
            cdp.send("Input.dispatchKeyEvent", event)
        cdp.send("Input.insertText", {"text": str(value)})
        result = cdp.send(
            "Runtime.evaluate",
            {
                "expression": "(() => { const e = document.activeElement; return e && 'value' in e ? String(e.value) : null; })()",
                "returnByValue": True,
            },
        )
        observed = result.get("result", {}).get("value") if isinstance(result, dict) else None
        return str(observed).strip() == str(value)

    def click_farm_template_mouse(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> None:
        """Use one mouse click for a fresh template as a guarded input fallback.

        Some browser portal HUD controls ignore synthetic touch while accepting
        normal mouse input.  Callers use this only after a verified touch has
        not changed state; gameplay controls continue to prefer touch.
        """
        left, top, width, height = bounds
        image_width, image_height = image_size
        if width <= 0 or height <= 0 or image_width <= 0 or image_height <= 0:
            raise ValueError("Farm template bounds không hợp lệ")
        # A raw page-level CDP mouse/touch can use the wrong origin when the
        # game canvas lives in an iframe. Resolve the canvas locator first and
        # express the freshly matched screenshot centre as canvas ratios; the
        # locator then applies the correct frame transform.
        self.click_game_surface_ratio(
            (left + width / 2) / image_width,
            (top + height / 2) / image_height,
        )

    def click_game_surface_native_ratio(self, x_ratio: float, y_ratio: float) -> None:
        """Send one native mouse click derived from a verified canvas ratio.

        Some portal WebGL builds ignore both Playwright and CDP input even
        though a fresh renderer capture matched the target.  This final
        fallback still derives its screen point from the live canvas geometry,
        never from a fixed desktop coordinate.
        """
        if sys.platform != "win32" or self.config.browser.headless:
            self.click_game_surface_ratio(x_ratio, y_ratio)
            return
        if not 0.0 <= x_ratio <= 1.0 or not 0.0 <= y_ratio <= 1.0:
            raise ValueError("Canvas ratios must be between 0 and 1")
        hwnd = self.window_handle or self._bind_native_window(retries=3)
        if hwnd is None:
            raise RuntimeError(f"Không tìm thấy cửa sổ Chrome của {self.profile.name}")
        _canvas, surface = self._canvas_or_viewport()
        renderer = get_renderer_rect(hwnd)
        viewport = self.page.viewport_size or self.page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        viewport_width = max(1.0, float(viewport["width"]))
        viewport_height = max(1.0, float(viewport["height"]))
        page_x = float(surface["x"]) + float(surface["width"]) * float(x_ratio)
        page_y = float(surface["y"]) + float(surface["height"]) * float(y_ratio)
        screen_x = renderer.left + round(page_x * renderer.width / viewport_width)
        screen_y = renderer.top + round(page_y * renderer.height / viewport_height)
        import ctypes

        user32 = ctypes.windll.user32
        # ``mouse_event`` sends a legacy, DPI-virtualised event on some
        # Windows setups.  The game then receives a click at a different
        # point even though the screen capture and matcher agree.  SendInput
        # uses the virtual desktop's physical coordinate system, the same one
        # used by the renderer rectangle captured just above.
        user32.BringWindowToTop(int(hwnd))
        user32.SetForegroundWindow(int(hwnd))
        time.sleep(0.05)

        class _MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class _InputUnion(ctypes.Union):
            _fields_ = [("mi", _MouseInput)]

        class _Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", ctypes.c_ulong), ("data", _InputUnion)]

        virtual_left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        virtual_top = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        virtual_width = max(1, user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        virtual_height = max(1, user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        absolute_x = round((screen_x - virtual_left) * 65535 / max(1, virtual_width - 1))
        absolute_y = round((screen_y - virtual_top) * 65535 / max(1, virtual_height - 1))
        move_flags = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
        inputs = (_Input * 3)(
            _Input(type=0, mi=_MouseInput(absolute_x, absolute_y, 0, move_flags, 0, None)),
            _Input(type=0, mi=_MouseInput(absolute_x, absolute_y, 0, 0x0002, 0, None)),
            _Input(type=0, mi=_MouseInput(absolute_x, absolute_y, 0, 0x0004, 0, None)),
        )
        sent = user32.SendInput(3, ctypes.byref(inputs), ctypes.sizeof(_Input))
        if sent != 3:
            raise ctypes.WinError(ctypes.get_last_error())

    def _farm_template_center(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> tuple[float, float]:
        left, top, width, height = bounds
        image_width, image_height = image_size
        if width <= 0 or height <= 0 or image_width <= 0 or image_height <= 0:
            raise ValueError("Farm template bounds không hợp lệ")
        _canvas, surface = self._canvas_or_viewport()
        x = float(surface["x"]) + float(surface["width"]) * (left + width / 2) / image_width
        y = float(surface["y"]) + float(surface["height"]) * (top + height / 2) / image_height
        return x, y

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
        # WindowFromPoint is advisory only.  Chrome's app-mode/WebGL renderer
        # can be reported under a compositor HWND unrelated to the profile
        # window even while the GDI capture below contains the live game.  A
        # hard failure here therefore stopped valid farm runs.  Capture first;
        # the non-empty pixels are the actual input for the template gate, and
        # no gameplay click is allowed until that fresh capture matches the
        # intended target.  A real overlay normally yields non-game pixels and
        # is rejected by that gate as Unknown rather than triggering a blind
        # click against the profile.
        png = capture_screen_region_png(region)
        if not _png_has_visible_content(png):
            raise RuntimeError("Ảnh màn hình game trống; hãy đưa cửa sổ profile ra màn hình")
        return png

    def _largest_canvas(self, frame: Any) -> tuple[Any, dict[str, float]]:
        candidate = self._visible_canvas(frame)
        if candidate is None:
            raise RuntimeError("Không tìm thấy canvas game đang hiển thị")
        return candidate

    def scroll_game_surface(self, delta_y: float) -> None:
        """Send a wheel event at the canvas centre without moving the real cursor."""
        _canvas, surface = self._canvas_or_viewport()
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
        _canvas, surface = self._canvas_or_viewport()
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

    def click_game_surface_ratio(self, x_ratio: float, y_ratio: float) -> None:
        """Mouse-click an exact point through the canvas locator in its frame.

        The game is hosted in an iframe on some portal variants. Raw page CDP
        coordinates can then use a different origin from a renderer capture.
        Playwright's canvas locator resolves the correct frame and transform,
        while ``position`` remains the requested canvas ratio.
        """
        if not 0.0 <= x_ratio <= 1.0 or not 0.0 <= y_ratio <= 1.0:
            raise ValueError("Canvas ratios must be between 0 and 1")
        canvas, surface = self._canvas_or_viewport()
        local_x = float(surface["width"]) * float(x_ratio)
        local_y = float(surface["height"]) * float(y_ratio)
        if canvas is not None:
            canvas.click(
                position={"x": local_x, "y": local_y},
                force=True,
                timeout=self.config.browser.startup_timeout_ms,
            )
            return

        # The WebGL OOPIF is composited into the page even when Chrome does
        # not attach it to Playwright's DOM frame tree. Page-level CDP input
        # is hit-tested by Chromium's compositor and reaches that child
        # surface at the same coordinates used for the verified screenshot.
        x = float(surface["x"]) + local_x
        y = float(surface["y"]) + local_y
        cdp = self._get_page_cdp_session(self.page)
        cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )

    def set_sync_source(self, enabled: bool) -> int:
        enabled = bool(enabled)
        if self._sync_source == enabled:
            # Followers already start with capture disabled. Reconfiguring all
            # of their frames when Sync is enabled can block a slow profile's
            # worker long enough for its input commands to pile up behind it.
            return self.sync_capture_frame_count() if enabled else 0
        self._sync_source = enabled
        self._configure_interaction_frames(force=True)
        return self._repair_and_count_sync_frames() if self._sync_source else 0

    def repair_synced_input_runtime(self) -> None:
        """Reconnect input dispatch after a follower frame/page navigation."""
        page = self.page
        self._detach_page_cdp_session()
        self._runtime_page = None
        self._configured_frames.clear()
        self._ensure_page_runtime(page)
        self._configure_interaction_frames(force=True)

    def _repair_and_count_sync_frames(self) -> int:
        """Re-arm input capture in retained/reconnected Chrome documents."""
        armed = 0
        for frame in self.page.frames:
            try:
                ready = frame.evaluate(
                    """([syncSource, inspectEnabled]) => {
                        if (!Array.isArray(window.__IK_SYNC_EVENTS)) window.__IK_SYNC_EVENTS = [];
                        if (!Array.isArray(window.__IK_COORDINATE_EVENTS)) window.__IK_COORDINATE_EVENTS = [];
                        // Profiles can survive a tool update. Repair the mode setter
                        // when an older in-page probe left only its event listeners.
                        if (typeof window.__IK_SET_INTERACTION_MODES !== 'function') {
                            window.__IK_SET_INTERACTION_MODES = (sync, inspect) => {
                                window.__IK_SYNC_SOURCE = Boolean(sync);
                                window.__IK_INSPECT_ENABLED = Boolean(inspect);
                            };
                        }
                        window.__IK_SET_INTERACTION_MODES(syncSource, inspectEnabled);
                        return Boolean(
                            window.__IK_INTERACTION_PROBE_INSTALLED &&
                            Array.isArray(window.__IK_SYNC_EVENTS) &&
                            typeof window.__IK_SET_INTERACTION_MODES === 'function' &&
                            window.__IK_SYNC_SOURCE === Boolean(syncSource)
                        );
                    }""",
                    [self._sync_source, self._inspector_enabled],
                )
                if ready:
                    armed += 1
                    # A repaired frame must remain in the normal configuration
                    # cache; otherwise every 40 ms poll would rewrite its state.
                    self._configured_frames[id(frame)] = (
                        f"{frame.url}|{self._sync_source}|{self._inspector_enabled}|"
                        f"{self._drag_item_visible}|{self._scrollbars_visible}"
                    )
            except Exception:
                self._configured_frames.pop(id(frame), None)
        return armed

    def sync_capture_frame_count(self) -> int:
        """Return the number of frames whose input probe is actively armed."""
        if not self._sync_source:
            return 0
        armed = 0
        for frame in self.page.frames:
            try:
                if frame.evaluate(
                    """() => Boolean(
                        window.__IK_INTERACTION_PROBE_INSTALLED &&
                        Array.isArray(window.__IK_SYNC_EVENTS) &&
                        typeof window.__IK_SET_INTERACTION_MODES === 'function' &&
                        window.__IK_SYNC_SOURCE === true
                    )"""
                ):
                    armed += 1
            except Exception:
                continue
        return armed

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
        self._window_handle = self._find_native_window()
        if self._window_handle is not None:
            set_taskbar_group(self._window_handle)
        return self._window_handle

    def _find_native_window(self) -> int | None:
        # Prefer the profile title because it also works for externally
        # configured CDP sessions. Fall back to the CDP-owning process when
        # Chrome has truncated, localized, or not yet applied that title.
        return find_chrome_window(self.profile.name) or find_chrome_window_for_process(
            getattr(self, "_managed_browser_pid", None)
        )

    def _bind_native_window(self, retries: int = 30) -> int | None:
        if self.config.browser.headless:
            return None
        for _attempt in range(max(1, retries)):
            hwnd = self._find_native_window()
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
        event_type = str(event.get("type", ""))
        if event_type in {"keydown", "keyup"}:
            self._apply_synced_keyboard_input(event, event_type)
            return
        box = self._synced_pointer_target_box(event)
        x, y = calculate_target_point(event, box)
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

    def _synced_pointer_target_box(self, event: dict[str, Any]) -> dict[str, float]:
        """Resolve the follower canvas, falling back to its live viewport.

        During iframe navigation Chrome can visibly retain the game frame
        while Playwright temporarily cannot enumerate its canvas. The game is
        fitted to the full renderer, so viewport-ratio dispatch is a safe
        fallback and prevents one slow follower from dropping the click.
        """
        try:
            frame = self._frame_for_input(event)
            canvas = event.get("canvas")
            if isinstance(canvas, dict):
                return self._canvas_box(frame, int(canvas.get("index", 0)))
            return self._frame_box(frame)
        except Exception:
            page = self.page
            viewport = page.viewport_size or page.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
            return {
                "x": 0.0,
                "y": 0.0,
                "width": float(viewport["width"]),
                "height": float(viewport["height"]),
            }

    def _apply_synced_keyboard_input(self, event: dict[str, Any], event_type: str) -> None:
        keyboard = event.get("keyboard")
        if not isinstance(keyboard, dict):
            return
        key = str(keyboard.get("key", ""))
        code = str(keyboard.get("code", ""))
        key_code = max(0, int(keyboard.get("key_code", 0) or 0))
        if not key and not code and not key_code:
            return
        modifiers = (
            (1 if keyboard.get("alt") else 0)
            | (2 if keyboard.get("ctrl") else 0)
            | (4 if keyboard.get("meta") else 0)
            | (8 if keyboard.get("shift") else 0)
        )
        params: dict[str, Any] = {
            "type": "keyDown" if event_type == "keydown" else "keyUp",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": key_code,
            "nativeVirtualKeyCode": key_code,
            "modifiers": modifiers,
            "location": max(0, int(keyboard.get("location", 0) or 0)),
            "autoRepeat": bool(keyboard.get("repeat", False)),
            "isKeypad": int(keyboard.get("location", 0) or 0) == 3,
        }
        # Printable keys need `text` to update focused inputs. Shortcuts must
        # not inject that character in addition to the Ctrl/Alt/Meta action.
        if event_type == "keydown" and len(key) == 1 and not (modifiers & 0b0111):
            params["text"] = key
            params["unmodifiedText"] = key
        self._get_page_cdp_session(self.page).send("Input.dispatchKeyEvent", params)

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
                frame.evaluate(GAME_FRAME_FIT_SCRIPT)
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
                # The portal can put the game canvas inside one or more plain
                # wrapper elements.  Resetting only ``body`` was insufficient
                # for that variant: its wrapper still retained the browser's
                # 8px layout gutter, visibly leaving a black frame around the
                # game.  Make every canvas ancestor fill the document and
                # make the largest canvas fill that resulting surface.
                frame.evaluate(
                    """() => {
                        if (!document.querySelector('canvas')) return;
                        const styleId = '__ik_auto_canvas_fit';
                        let style = document.getElementById(styleId);
                        if (!style) {
                            style = document.createElement('style');
                            style.id = styleId;
                            (document.head || document.documentElement).appendChild(style);
                        }
                        const canvases = [...document.querySelectorAll('canvas')];
                        const canvas = canvases.sort((left, right) =>
                            (right.width * right.height) - (left.width * left.height)
                        )[0];
                        if (!canvas) return;
                        for (let node = canvas; node && node !== document.documentElement; node = node.parentElement) {
                            node.style.setProperty('margin', '0', 'important');
                            node.style.setProperty('padding', '0', 'important');
                            node.style.setProperty('border', '0', 'important');
                            node.style.setProperty('width', '100%', 'important');
                            node.style.setProperty('height', '100%', 'important');
                            node.style.setProperty('max-width', 'none', 'important');
                            node.style.setProperty('max-height', 'none', 'important');
                            node.style.setProperty('overflow', 'hidden', 'important');
                        }
                        style.textContent = `
                            html, body { margin: 0 !important; padding: 0 !important; width: 100% !important; height: 100% !important; overflow: hidden !important; background: #000 !important; }
                            canvas { display: block !important; margin: 0 !important; padding: 0 !important; border: 0 !important; width: 100% !important; height: 100% !important; max-width: none !important; max-height: none !important; }
                        `;
                    }"""
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
        frames = list(self.page.frames)
        exact_matches = [frame for frame in frames if source_url and frame.url == source_url]
        host_matches = [
            frame
            for frame in frames
            if source_host
            and (urlsplit(frame.url).hostname or "").lower() == source_host.lower()
        ]
        # A portal may contain several same-host frames. Selecting the last
        # one can route a valid game event into a wrapper/advert frame. Pick
        # the exact URL first and, for canvas input, require the candidate
        # with the largest visible canvas.
        for candidates in (exact_matches, host_matches):
            resolved = self._best_input_frame(candidates, event)
            if resolved is not None:
                return resolved
        return self.find_frame()

    @staticmethod
    def _best_input_frame(frames: list[Frame], event: dict[str, Any]) -> Frame | None:
        if not frames:
            return None
        if not isinstance(event.get("canvas"), dict):
            return frames[-1]
        scored: list[tuple[float, Frame]] = []
        for frame in frames:
            try:
                canvases = frame.locator("canvas")
                areas = []
                for index in range(canvases.count()):
                    box = canvases.nth(index).bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        areas.append(float(box["width"]) * float(box["height"]))
                if areas:
                    scored.append((max(areas), frame))
            except Exception:
                continue
        return max(scored, key=lambda item: item[0])[1] if scored else None

    def _canvas_box(self, frame: Frame, index: int) -> dict[str, float]:
        canvases = frame.locator("canvas")
        boxes: list[dict[str, float]] = []
        for candidate_index in range(canvases.count()):
            box = canvases.nth(candidate_index).bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                boxes.append(box)
        if boxes:
            # Canvas order is not stable across accounts, Chrome versions, or
            # machines. The source index can therefore point at a tiny overlay
            # on a follower. The game is consistently the largest canvas.
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
        raise RuntimeError("Không xác định được vị trí frame đích để đồng bộ thao tác")

    def pump(self, milliseconds: int = 50) -> None:
        if self._page is not None and not self._page.is_closed():
            self._page.wait_for_timeout(milliseconds)
            self._configure_interaction_frames()

    def close(self, *, close_browser: bool = False) -> None:
        """Detach from a profile, optionally closing its Chrome window.

        Closing the IK Auto dashboard must only detach: its profiles are
        intentionally retained for an update/restart.  The explicit
        ``Đóng tabs`` command, however, owns the user's intent to close the
        individual Chrome profile window and must close the persistent
        browser context even though Chrome was launched as a detached process.
        """
        self._closing = True
        try:
            if close_browser:
                # ``connect_over_cdp`` attaches to Chrome's default context.
                # Closing that BrowserContext is not a reliable way to close
                # the externally launched browser and may simply be ignored.
                # Browser.close is the DevTools command for terminating this
                # exact per-profile Chrome process.
                try:
                    if self._page is not None and not self._page.is_closed():
                        self._get_page_cdp_session(self._page).send("Browser.close", {})
                except Exception:
                    # If the browser-level command is unavailable, close all
                    # pages explicitly. A managed app-mode profile normally
                    # has one page, whose close also closes its window.
                    if self._context is not None:
                        for page in tuple(self._context.pages):
                            try:
                                if not page.is_closed():
                                    page.close(run_before_unload=False)
                            except Exception:
                                continue
            self._detach_page_cdp_session()
            # Some focused vision tests construct a lightweight session with
            # ``__new__`` and therefore do not run ``__init__``.  Treat those
            # sessions as externally owned, the same safe default used for a
            # detached profile Chrome.
            if (
                (close_browser or getattr(self, "_owns_browser_process", False))
                and self._context is not None
            ):
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
