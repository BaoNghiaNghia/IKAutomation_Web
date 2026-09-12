from __future__ import annotations

import base64
import contextlib
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

from ik_chrome_auto.chrome_preferences import suppress_browser_prompts
from ik_chrome_auto.config import is_allowed_url
from ik_chrome_auto.image_utils import RGBImage, decode_png
from ik_chrome_auto.input_engine import ProfileInputEngine, ViewportPoint
from ik_chrome_auto.input_helpers import (
    GAME_REFERENCE_HEIGHT,
    GAME_REFERENCE_WIDTH,
    CanvasReferencePoint,
    CanvasTransformSnapshot,
    control_center_reference_point,
)
from ik_chrome_auto.interaction import (
    INTERACTION_PROBE,
    calculate_target_point,
    validate_viewport,
)
from ik_chrome_auto.models import AppConfig, ProfileConfig, ProfileMode
from ik_chrome_auto.reader import GameDataReader, redact_url
from ik_chrome_auto.windows import (
    WindowRect,
    capture_screen_region_png,
    find_chrome_window,
    find_chrome_window_for_process,
    find_tcp_listener_process,
    get_process_command_line,
    get_renderer_rect,
    get_window_rect,
    is_window,
    is_window_minimized,
    move_window_position,
    move_window_renderer,
    raise_window_above_profile_peers,
    set_taskbar_group,
    set_window_minimized,
)
from ik_chrome_auto.windows import (
    set_topmost as set_native_topmost,
)


@dataclass(frozen=True, slots=True)
class SyncSourceStatus:
    """Result of arming the *actual* master input surface.

    ``armed_frame_count`` is deliberately diagnostic only.  A portal document
    can accept the probe while the game lives in a nested OOPIF, so callers
    must use ``ready`` (verified input roots) for the Sync lifecycle.
    """

    armed_frame_count: int = 0
    input_frame_count: int = 0
    game_frame_count: int = 0
    ready: bool = False
    failure_reason: str = ""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.armed_frame_count == other
        if isinstance(other, SyncSourceStatus):
            return (
                self.armed_frame_count, self.input_frame_count, self.game_frame_count,
                self.ready, self.failure_reason,
            ) == (
                other.armed_frame_count, other.input_frame_count, other.game_frame_count,
                other.ready, other.failure_reason,
            )
        return NotImplemented

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright


KNOWN_CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

_PROFILE_CDP_PORT_MIN = 21000
_PROFILE_CDP_PORT_SPAN = 20_000
_GAME_SURFACE_WIDTH = 1280.0
_GAME_SURFACE_HEIGHT = 720.0
_FARM_INPUT_FOCUS_DELAY_SECONDS = 0.25
_FARM_INPUT_ACTION_DELAY_SECONDS = 0.12
_FARM_INPUT_VERIFY_TIMEOUT_SECONDS = 0.8
_PORTAL_LOGIN_URL = "https://ik.playfun.vn/login-game"
_PORTAL_DIRECT_PLAY_ERROR_CODE = 141303
_PORTAL_DIRECT_PLAY_ERROR_MESSAGE = 211155


def _normalized_windows_path(value: str | Path) -> str:
    """Compare Chrome command-line paths with Windows filesystem semantics."""
    return os.path.normcase(os.path.normpath(str(value))).rstrip("\\/")


def _command_line_user_data_dir(command_line: str) -> str | None:
    """Extract ``--user-data-dir`` without relying on argument ordering."""
    match = re.search(
        r"--user-data-dir(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
        command_line,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return next((value for value in match.groups() if value is not None), None)


def _fixed_game_surface_box() -> dict[str, float]:
    """Return the coordinate system used while automation rerenders at 720p."""
    return {
        "x": 0.0,
        "y": 0.0,
        "width": _GAME_SURFACE_WIDTH,
        "height": _GAME_SURFACE_HEIGHT,
    }


def _origin_surface_box(box: dict[str, float]) -> dict[str, float]:
    """Keep live surface dimensions while deliberately discarding iframe offsets."""
    return {
        "x": 0.0,
        "y": 0.0,
        "width": max(1.0, float(box["width"])),
        "height": max(1.0, float(box["height"])),
    }


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
    "input[type='text'][name*='account' i]",
    "input[type='text'][name*='phone' i]",
    "input[type='text'][name*='login' i]",
    "input[name*='email' i]",
    "input[name*='user' i]",
    "input[name*='account' i]",
    "input[name*='phone' i]",
    "input[placeholder*='Email' i]",
    "input[placeholder*='Tên đăng nhập' i]",
    "input[placeholder*='Tài khoản' i]",
    "input[placeholder*='Số điện thoại' i]",
)
LOGIN_PASSWORD_SELECTORS = (
    "input[autocomplete='current-password']",
    "input[type='password']",
)
LOGIN_BUTTON_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button[id*='login' i]",
    "button[class*='login' i]",
    "[role='button'][id*='login' i]",
    "[role='button'][class*='login' i]",
)
_AUTO_LOGIN_RETRY_SECONDS = 2.5
_AUTO_LOGIN_WINDOW_SECONDS = 180.0


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
            width: var(--ik-auto-game-frame-width, 100vw) !important;
            height: var(--ik-auto-game-frame-height, 100vh) !important;
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
GAME_FRAME_SIZE_SCRIPT = r"""([enabled, width, height]) => {
    const root = document.documentElement;
    if (!root) return;
    if (enabled) {
        root.style.setProperty('--ik-auto-game-frame-width', `${width}px`);
        root.style.setProperty('--ik-auto-game-frame-height', `${height}px`);
    } else {
        root.style.removeProperty('--ik-auto-game-frame-width');
        root.style.removeProperty('--ik-auto-game-frame-height');
    }
}"""


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
        self._managed_cdp_endpoint: str | None = None
        self._topmost = False
        self._configured_frames: dict[int, str] = {}
        # Sync polling is a hot path.  Keep the roots successfully armed by
        # the source command instead of rediscovering nested iframe locators
        # for every idle worker tick.
        self._sync_capture_roots: list[Any] = []
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
        # A synchronized gesture must use one immutable, canvas-local follower
        # transform. Re-reading an iframe during pointerdown/move/up can
        # produce different sizes while Chrome lays out dozens of profiles.
        self._sync_pointer_target_box: dict[str, float] | None = None
        self._sync_last_target_box: dict[str, float] | None = None
        self._sync_pressed_buttons: set[str] = set()
        self._sync_pressed_keys: dict[str, dict[str, Any]] = {}
        # Login iframes arrive noticeably later when dozens of Chrome
        # profiles start together. Keep a bounded background retry window
        # instead of relying on the first DOMContentLoaded snapshot.
        self._auto_login_completed = False
        self._auto_login_deferred = True
        self._auto_login_next_at = 0.0
        self._auto_login_deadline = time.monotonic() + _AUTO_LOGIN_WINDOW_SECONDS
        # Normal profile windows retain their responsive iframe dimensions.
        # Only an active automation renderer lease forces the game to 1280x720.
        self._automation_game_frame_fixed = False

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

    def start(
        self,
        *,
        navigate: bool = True,
        resize: bool = True,
        reattach: bool = False,
    ) -> Page:
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
        # A worker may be created long before the user opens this profile.
        # Start the retry window here, not only in ``goto()``, so an already
        # loaded login page still receives its full delayed-form retry period.
        if navigate:
            self._reset_auto_login_window()
        self._attach_lifecycle()
        if reattach:
            # A profile retained across a Tool update already has its page,
            # viewport and game DOM. Replaying startup scripts into every
            # frame made 40+ simultaneous CDP reconnects needlessly slow.
            self._page = self._choose_page(create=True)
            self._track_page(self._page)
            self._bind_native_window(retries=3)
            return self.page
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
        # Install before any later portal navigation. Normal profile windows
        # remain responsive; an automation lease temporarily supplies the
        # 1280x720 CSS variables used by this stylesheet.
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
        if resize and self.config.browser.auto_resize:
            self.resize(
                self.config.browser.viewport_width,
                self.config.browser.viewport_height,
            )
        if navigate and not self._is_target(self.page.url):
            self.goto()
        self._bind_native_window()
        return self.page

    def reset_reconnected_interaction_modes(self) -> None:
        """Turn off listeners left by a previous Tool instance cheaply."""
        # Disabling must never walk nested FrameLocators. A stale cross-origin
        # locator can wait for Playwright's default 60s timeout and block the
        # master worker queue, leaving a later enable request in STARTING.
        # Cached armed roots cover the documents we changed; direct frames are
        # a cheap best-effort fallback after a page navigation.
        roots: list[Any] = list(getattr(self, "_sync_capture_roots", ()))
        with contextlib.suppress(Exception):
            roots.extend(self.page.frames)
        seen: set[int] = set()
        for frame in roots:
            if id(frame) in seen:
                continue
            seen.add(id(frame))
            try:
                self._evaluate_frame_root(
                    frame,
                    """() => {
                        window.__IK_SYNC_SOURCE = false;
                        window.__IK_INSPECT_ENABLED = false;
                        window.__IK_SET_INTERACTION_MODES?.(false, false);
                    }"""
                )
            except Exception:
                continue

    def can_attach_existing_browser(self) -> bool:
        """Check whether this configured profile already has a live CDP port.

        This never launches Chrome.  It is used on IK Auto startup so an
        update can reconnect to retained browser windows independently.
        """
        if self.profile.mode == ProfileMode.MANAGED:
            endpoint = self._discover_managed_cdp_endpoint()
            if endpoint is not None:
                self._managed_cdp_endpoint = endpoint
                return True
            return False
        return bool(self.profile.cdp_url and _cdp_endpoint_is_ready(self.profile.cdp_url))

    def _managed_port_candidates(self) -> tuple[int, ...]:
        configured = self.profile.cdp_port or _profile_cdp_port(self.profile.id)
        legacy = _profile_cdp_port(self.profile.id)
        return (configured,) if configured == legacy else (configured, legacy)

    def _discover_managed_cdp_endpoint(self) -> str | None:
        """Find a live, identity-matching endpoint for this managed profile."""
        expected_dir = (
            _normalized_windows_path(self.profile.user_data_dir.resolve())
            if self.profile.user_data_dir
            else None
        )
        for port in self._managed_port_candidates():
            endpoint = f"http://127.0.0.1:{port}"
            if not _cdp_endpoint_is_ready(endpoint):
                continue
            pid = find_tcp_listener_process(port)
            command_line = get_process_command_line(pid)
            if expected_dir is not None:
                actual_dir = _command_line_user_data_dir(command_line) if command_line else None
                # Reject a positively identified foreign Chrome process. On
                # hardened Windows where the command line is inaccessible,
                # the collision-free configured port remains the compatible
                # identity signal for legacy profiles.
                if actual_dir is not None and _normalized_windows_path(actual_dir) != expected_dir:
                    continue
            self._managed_browser_pid = pid
            return endpoint
        return None

    def _start_managed(self) -> None:
        assert self._playwright is not None
        if self.profile.user_data_dir is None:
            raise RuntimeError(f"Profile {self.profile.id} thiếu user_data_dir")
        self.profile.user_data_dir.mkdir(parents=True, exist_ok=True)
        suppress_browser_prompts(self.profile.user_data_dir)
        chrome = find_chrome(self.config.browser.chrome_executable)
        if chrome is None:
            raise RuntimeError("Không tìm thấy Google Chrome; sửa browser.chrome_executable")
        port = self.profile.cdp_port or _profile_cdp_port(self.profile.id)
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
        existing_endpoint = self._discover_managed_cdp_endpoint()
        if existing_endpoint is not None:
            self._connect_cdp(existing_endpoint)
            return
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
        contexts = list(self._browser.contexts)
        if not contexts:
            raise RuntimeError("Chrome CDP không có browser context")
        self._context = self._select_cdp_context(contexts)

    def _select_cdp_context(self, contexts: list[BrowserContext]) -> BrowserContext:
        """Select the one controlled context; never silently pick arbitrary CDP state."""
        if len(contexts) == 1:
            return contexts[0]
        ranked = [
            (sum(1 for page in context.pages if not page.is_closed() and self._is_target(page.url)), context)
            for context in contexts
        ]
        highest = max(score for score, _context in ranked)
        matches = [context for score, context in ranked if score == highest]
        if highest > 0 and len(matches) == 1:
            return matches[0]
        raise RuntimeError(
            "Chrome CDP có nhiều browser context không thể xác định profile; "
            "hãy đóng tab/incognito không liên quan rồi thử lại"
        )

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
            with contextlib.suppress(Exception):
                session.detach()

    def is_alive(self) -> bool:
        if self._externally_closed or self._context is None:
            return False
        return self._page is not None and not self._page.is_closed()

    def _choose_page(self, *, create: bool = False) -> Page:
        pages = [page for page in self.context.pages if not page.is_closed()]
        target_pages = [page for page in pages if self._is_target(page.url)]
        if target_pages:
            target_host = urlsplit(self.config.target_url).hostname
            target_path = urlsplit(self.config.target_url).path.rstrip("/")
            return max(
                target_pages,
                key=lambda page: (
                    urlsplit(page.url).hostname == target_host,
                    urlsplit(page.url).path.rstrip("/") == target_path,
                    urlsplit(page.url).path.rstrip("/") in {"/login-game", "/play-game"},
                ),
            )
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
            with contextlib.suppress(Exception):
                page.close(run_before_unload=False)

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
        with contextlib.suppress(Exception):
            self._page.evaluate(self._profile_title_script())

    def goto(self, url: str | None = None) -> None:
        target = url or self.config.target_url
        self._reset_auto_login_window()
        self.page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=self.config.browser.startup_timeout_ms,
        )
        # The portal's direct ``/play-game`` route now responds with a raw
        # JSON error for a fresh/expired game session.  Leaving that response
        # in the app window hides the login form and prevents the automatic
        # username/password recovery from running.  Redirect only this known
        # server response to the official login route; never mask a different
        # game response or page failure.
        if self._is_portal_direct_play_session_error(target):
            self.page.goto(
                _PORTAL_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=self.config.browser.startup_timeout_ms,
            )
        self._configure_interaction_frames(force=True)
        self._apply_profile_title()

    def _is_portal_direct_play_session_error(self, target: str) -> bool:
        target_path = urlsplit(target).path.rstrip("/")
        if target_path != "/play-game":
            return False
        try:
            page = self._page
            if page is None:
                return False
            body = page.locator("body").inner_text(timeout=1_000).strip()
            payload = json.loads(body)
        except Exception:
            return False
        return (
            isinstance(payload, dict)
            and payload.get("code") == _PORTAL_DIRECT_PLAY_ERROR_CODE
            and payload.get("msg") == _PORTAL_DIRECT_PLAY_ERROR_MESSAGE
        )

    def _reset_auto_login_window(self) -> None:
        """Give every newly opened/navigated profile a fresh login window."""
        self._auto_login_completed = False
        self._auto_login_next_at = 0.0
        self._auto_login_deadline = time.monotonic() + _AUTO_LOGIN_WINDOW_SECONDS
        self._auto_login_deferred = True

    def begin_auto_login(self) -> bool:
        """Start login only after the bulk Chrome startup phase is complete."""
        self._reset_auto_login_window()
        self._auto_login_deferred = False
        return self.auto_login_if_needed()

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
        # The portal sometimes places its login form one or more iframes below
        # the page frame.  ``page.frames`` is not complete in that situation.
        # Reuse the frame walker used by canvas discovery, while retaining the
        # host allow-list before any credential is entered.
        seen_frames: set[int] = set()
        # A stale nested locator may wait for Playwright's 60s default timeout.
        # Disable only roots we armed plus cheap direct frames, never a deep
        # traversal that can block the master worker's command queue.
        roots: list[Any] = list(getattr(self, "_sync_capture_roots", ()))
        with contextlib.suppress(Exception):
            roots.extend(self.page.frames)
        seen: set[int] = set()
        for frame in roots:
            if id(frame) in seen:
                continue
            seen.add(id(frame))
            frame_identity = id(frame)
            if frame_identity in seen_frames:
                continue
            seen_frames.add(frame_identity)
            frame_url = self._automation_root_url(frame)
            if not frame_url or not is_allowed_url(
                frame_url, self.config.capture.allowed_hosts
            ):
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
                login_button = self._first_visible_login_button(frame)
                time.sleep(random.uniform(0.12, 0.24))
                if login_button is not None:
                    login_button.click(timeout=3_000)
                else:
                    # The legacy portal sometimes renders no semantic button
                    # but still submits the password field on Enter.
                    password.press("Enter", timeout=3_000)
                # A click can be intercepted by an overlay or rejected by a
                # client-side validation rule.  Treat it as successful only
                # after the actual form disappears; otherwise the idle worker
                # will retry rather than silently abandoning this profile.
                time.sleep(0.45)
                self._auto_login_completed = not self._login_form_is_visible(frame)
                if not self._auto_login_completed:
                    self._auto_login_next_at = (
                        time.monotonic() + _AUTO_LOGIN_RETRY_SECONDS
                    )
                return True
            except Exception:
                # Several login/advert frames can coexist. A stale candidate
                # must not prevent the valid frame below it from being tried.
                continue
        return False

    @staticmethod
    def _automation_root_url(root: Any) -> str | None:
        """Read a frame URL for both ``Frame`` and nested ``FrameLocator``.

        Playwright ``FrameLocator`` deliberately has no ``url`` property.  It
        is still a valid root for the form selectors, so read its document URL
        inside that iframe before applying the credential host allow-list.
        """
        direct_url = getattr(root, "url", None)
        if isinstance(direct_url, str) and direct_url:
            return direct_url
        try:
            located_url = root.locator("html").evaluate(
                "() => window.location.href"
            )
        except Exception:
            return None
        return located_url if isinstance(located_url, str) and located_url else None

    def _login_form_is_visible(self, frame: Frame) -> bool:
        """Return whether the visible credential pair remains after submit."""
        try:
            return (
                self._first_visible_input(frame, LOGIN_USERNAME_SELECTORS) is not None
                and self._first_visible_input(frame, LOGIN_PASSWORD_SELECTORS) is not None
            )
        except Exception:
            # A detached/navigated frame means this submitted form is no
            # longer a usable candidate, so do not repeat the credentials.
            return False

    @staticmethod
    def _first_visible_login_button(frame: Frame) -> Any | None:
        try:
            button = frame.get_by_role(
                "button",
                name=re.compile(r"(đăng nhập|login|sign in)", re.IGNORECASE),
            ).first
            if button.count() and button.is_visible(timeout=500):
                return button
        except Exception:
            pass
        return ChromeProfileSession._first_visible_input(
            frame, LOGIN_BUTTON_SELECTORS
        )

    def _retry_auto_login_if_due(self) -> bool:
        """Retry delayed login forms without blocking bulk profile startup."""
        if getattr(self, "_auto_login_deferred", False) or getattr(self, "_auto_login_completed", False):
            return False
        now = time.monotonic()
        deadline = float(getattr(self, "_auto_login_deadline", 0.0) or 0.0)
        if deadline and now >= deadline:
            return False
        next_at = float(getattr(self, "_auto_login_next_at", 0.0) or 0.0)
        if now < next_at:
            return False
        self._auto_login_next_at = now + _AUTO_LOGIN_RETRY_SECONDS
        return self.auto_login_if_needed()

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

    def _set_automation_game_frame_size(
        self, enabled: bool, width: int = 1280, height: int = 720
    ) -> None:
        """Toggle the fixed iframe size used only during a renderer lease."""
        self._automation_game_frame_fixed = bool(enabled)
        for page in self.context.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                try:
                    frame.evaluate(
                        GAME_FRAME_SIZE_SCRIPT,
                        [bool(enabled), int(width), int(height)],
                    )
                except Exception:
                    continue

    def end_automation_game_frame(self) -> None:
        """Return the iframe to its normal responsive dimensions."""
        self._set_automation_game_frame_size(False)

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
        self._set_automation_game_frame_size(True, width, height)
        if sys.platform != "win32" or self.config.browser.headless:
            return None
        try:
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
            if resized:
                move_window_renderer(
                    hwnd,
                    outer.left,
                    outer.top,
                    width,
                    height,
                    topmost=self._topmost,
                )
            # The active renderer must cover the compact profile grid while
            # Farm/Monitoring owns it. This changes only sibling z-order, not
            # the user's permanent “always on top” preference.
            raise_window_above_profile_peers(hwnd)
            # Chromium needs a short settle period before its WebGL canvas reflects
            # the native renderer dimensions in a DevTools screenshot.
            self.pump(220)
            return layout
        except Exception:
            self.end_automation_game_frame()
            raise

    def restore_automation_renderer(self, layout: AutomationRendererLayout | None) -> None:
        """Restore the compact grid geometry saved for a temporary renderer."""
        self.end_automation_game_frame()
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
    def _evaluate_frame_root(root: Any, script: str, argument: Any = None) -> Any:
        """Evaluate in either a Playwright Frame or a nested FrameLocator.

        ``Locator.content_frame`` returns a FrameLocator in current Playwright
        versions. A FrameLocator has no ``evaluate`` method, so Sync previously
        skipped the deepest game iframe and never observed user input. Its
        ``html`` locator executes JavaScript in that nested document.
        """
        evaluate = getattr(root, "evaluate", None)
        if callable(evaluate):
            return evaluate(script) if argument is None else evaluate(script, argument)

        html = root.locator("html")
        return html.evaluate(script) if argument is None else html.evaluate(script, argument)

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
                if getattr(self, "_automation_game_frame_fixed", False):
                    return _fixed_game_surface_box()
                return _origin_surface_box(candidate[1])
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
            if getattr(self, "_automation_game_frame_fixed", False):
                return _fixed_game_surface_box()
            return _origin_surface_box(box)
        raise RuntimeError("Không xác định được vùng click của frame/canvas")

    def _viewport_surface_box(self) -> dict[str, float]:
        # The game is fixed to 1280x720 only while its automation renderer is
        # leased. Compact/normal windows keep their live responsive size.
        if getattr(self, "_automation_game_frame_fixed", False):
            return _fixed_game_surface_box()
        page = self.page
        viewport = page.viewport_size or page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        return _origin_surface_box(viewport)

    def _canvas_or_viewport(self) -> tuple[Any | None, dict[str, float]]:
        frame = self.find_frame()
        try:
            canvas, measured_box = self._largest_canvas(frame)
            if getattr(self, "_automation_game_frame_fixed", False):
                return canvas, _fixed_game_surface_box()
            return canvas, _origin_surface_box(measured_box)
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
                needs_fixed_surface_clip = False
                if canvas is None:
                    viewport = page.viewport_size or page.evaluate(
                        "() => ({width: window.innerWidth, height: window.innerHeight})"
                    )
                    needs_fixed_surface_clip = (
                        abs(float(box["width"]) - float(viewport["width"])) > 0.5
                        or abs(float(box["height"]) - float(viewport["height"])) > 0.5
                    )
                if canvas is not None or needs_fixed_surface_clip:
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
            except Exception as error:
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
                    raise RuntimeError("Chrome returned an empty game screenshot") from error
                if not _png_has_visible_content(png):
                    raise RuntimeError("Chrome returned a black game screenshot") from error
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
            "x": round(float(box["width"]) * left, 4),
            "y": round(float(box["height"]) * top, 4),
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

    def click_game_control(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
        *,
        input_kind: str = "touch",
    ) -> str:
        """Click the exact centre of a control found in a fresh game capture.

        Detection coordinates always belong to the captured game surface.
        Convert them once into the canonical 1280x720 canvas origin and send
        them through the single renderer input dispatcher.
        """
        point = control_center_reference_point(bounds, image_size)
        self.dispatch_game_surface_point(point.x, point.y, input_kind=input_kind)
        return f"cdp_{input_kind}_canvas_ratio"

    def click_farm_control(
        self,
        bounds: tuple[int, int, int, int],
        image_size: tuple[int, int],
        *,
        input_kind: str = "touch",
    ) -> str:
        """Compatibility wrapper for callers using the former Farm-only name."""
        return self.click_game_control(bounds, image_size, input_kind=input_kind)

    def tap_farm_template(self, bounds: tuple[int, int, int, int], image_size: tuple[int, int]) -> None:
        """Compatibility wrapper for the centralized farm click helper."""
        self.click_farm_control(bounds, image_size, input_kind="touch")

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
        try:
            point = control_center_reference_point(bounds, image_size)
        except ValueError:
            return None
        # Prefer the actual numeric DOM input when the portal exposes it. This
        # avoids relying on compositor timing for an HTML control layered over
        # the WebGL canvas and remains profile-local while windows overlap.
        value = self._focus_numeric_farm_input_dom(point.x, point.y)
        if value is not None:
            time.sleep(_FARM_INPUT_FOCUS_DELAY_SECONDS)
            value = self._focused_farm_input_value() or value
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return None

        # Fallback for portal variants whose editor is not exposed in the DOM.
        # Chromium hit-tests the canvas-relative viewport point without using
        # the native cursor or bringing this profile window to the foreground.
        self.dispatch_game_surface_point(
            point.x,
            point.y,
            input_kind="mouse",
            viewport_hit_test=True,
        )
        time.sleep(_FARM_INPUT_FOCUS_DELAY_SECONDS)
        deadline = time.monotonic() + _FARM_INPUT_VERIFY_TIMEOUT_SECONDS
        while True:
            value = self._focused_farm_input_value()
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                if time.monotonic() >= deadline:
                    return None
                time.sleep(_FARM_INPUT_ACTION_DELAY_SECONDS)

    def _focus_numeric_farm_input_dom(
        self,
        reference_x: float,
        reference_y: float,
    ) -> str | None:
        """Focus the visible numeric input nearest a canvas-relative point."""
        expression = """
        ({ referenceX, referenceY, referenceWidth, referenceHeight }) => {
          const canvases = Array.from(document.querySelectorAll('canvas')).flatMap((canvas) => {
            const rect = canvas.getBoundingClientRect();
            const style = getComputedStyle(canvas);
            if (
              rect.width < 10 || rect.height < 10 ||
              style.display === 'none' || style.visibility === 'hidden'
            ) return [];
            return [{ canvas, rect, area: rect.width * rect.height }];
          }).sort((left, right) => right.area - left.area);
          if (!canvases.length) return null;
          const canvasRect = canvases[0].rect;
          const targetX = canvasRect.left + canvasRect.width * referenceX / referenceWidth;
          const targetY = canvasRect.top + canvasRect.height * referenceY / referenceHeight;
          const candidates = Array.from(document.querySelectorAll('input')).flatMap((element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            const value = String(element.value ?? '').trim();
            if (
              rect.width < 10 || rect.height < 10 ||
              style.display === 'none' || style.visibility === 'hidden' ||
              !/^\\d{1,4}$/.test(value)
            ) return [];
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            return [{ element, distance: Math.hypot(centerX - targetX, centerY - targetY) }];
          }).sort((left, right) => left.distance - right.distance);
          if (!candidates.length || candidates[0].distance > 120) return null;
          const input = candidates[0].element;
          input.focus({ preventScroll: true });
          input.click();
          try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) {}
          return String(input.value ?? '');
        }
        """
        try:
            roots = tuple(reversed(self._frame_roots()))
        except Exception:
            return None
        for root in roots:
            try:
                value = root.evaluate(
                    expression,
                    {
                        "referenceX": float(reference_x),
                        "referenceY": float(reference_y),
                        "referenceWidth": GAME_REFERENCE_WIDTH,
                        "referenceHeight": GAME_REFERENCE_HEIGHT,
                    },
                )
            except Exception:
                continue
            if value is not None and re.fullmatch(r"\d{1,4}", str(value).strip()):
                return str(value).strip()
        return None

    def _focused_farm_input_value(self) -> str | None:
        """Read the focused input from the document that owns the game UI.

        The coordinate inputs live inside the GTArcade iframe. Evaluating
        ``document.activeElement`` through the page-level CDP session only
        sees the outer iframe element and therefore always returned null.
        Walk the known frame roots from inner to outer and accept the first
        focused element that exposes a value.
        """
        expression = "() => { const e = document.activeElement; return e && 'value' in e ? String(e.value) : null; }"
        for root in reversed(self._frame_roots()):
            try:
                value = root.evaluate(expression)
            except Exception:
                continue
            if value is not None:
                return str(value)
        return None

    def replace_focused_farm_input(self, value: int) -> bool:
        """Replace one focused X/Y value from its end and confirm the result.

        The game coordinate editor does not handle Ctrl+A consistently across
        portal profiles. Read the current value, move its caret to the end,
        delete every existing character with Backspace, then insert the new
        text directly through CDP. ``Input.insertText`` provides paste-like
        input without reading or overwriting the user's system clipboard.
        """
        if value < 0:
            return False
        current = self._focused_farm_input_value()
        if current is None:
            return False
        cdp = self._get_page_cdp_session(self.page)
        self._press_cdp_key("End", "End", 35, cdp=cdp)
        time.sleep(_FARM_INPUT_ACTION_DELAY_SECONDS)
        for _ in str(current):
            self._press_cdp_key("Backspace", "Backspace", 8, cdp=cdp)
            time.sleep(_FARM_INPUT_ACTION_DELAY_SECONDS)
        ProfileInputEngine.insert_text(cdp, str(value))
        time.sleep(_FARM_INPUT_ACTION_DELAY_SECONDS)
        deadline = time.monotonic() + _FARM_INPUT_VERIFY_TIMEOUT_SECONDS
        while True:
            observed = self._focused_farm_input_value()
            if str(observed).strip() == str(value):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_FARM_INPUT_ACTION_DELAY_SECONDS)

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
        self.click_farm_control(bounds, image_size, input_kind="mouse")

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
        left = renderer.left
        top = renderer.top
        right = renderer.left + round(float(box["width"]) * scale_x)
        bottom = renderer.top + round(float(box["height"]) * scale_y)
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
        _canvas, transform = self._game_surface_transform_snapshot()
        x, y = transform.to_viewport(
            CanvasReferencePoint(GAME_REFERENCE_WIDTH / 2, GAME_REFERENCE_HEIGHT / 2)
        )
        ProfileInputEngine.wheel(
            self._get_page_cdp_session(self.page),
            ViewportPoint(x, y),
            0,
            float(delta_y),
        )

    def press_escape(self) -> None:
        """Dismiss the current WebGL popup without focusing the real window."""
        self._press_cdp_key("Escape", "Escape", 27)

    def _press_cdp_key(
        self,
        key: str,
        code: str,
        virtual_key_code: int,
        *,
        cdp: Any | None = None,
    ) -> None:
        """Send one complete profile-local key press through CDP."""
        ProfileInputEngine.key_press(
            cdp or self._get_page_cdp_session(self.page),
            key=key,
            code=code,
            virtual_key_code=virtual_key_code,
        )

    def tap_game_surface_ratio(self, x_ratio: float, y_ratio: float) -> None:
        """Compatibility wrapper for a canvas-relative touch."""
        self.dispatch_game_surface_point(
            x_ratio * GAME_REFERENCE_WIDTH,
            y_ratio * GAME_REFERENCE_HEIGHT,
            input_kind="touch",
        )

    def click_game_surface_ratio(self, x_ratio: float, y_ratio: float) -> None:
        """Compatibility wrapper for a canvas-relative mouse click."""
        self.dispatch_game_surface_point(
            x_ratio * GAME_REFERENCE_WIDTH,
            y_ratio * GAME_REFERENCE_HEIGHT,
            input_kind="mouse",
        )

    def dispatch_game_surface_mouse_ratio(self, x_ratio: float, y_ratio: float) -> None:
        """Compatibility wrapper for the centralized mouse dispatcher."""
        self.dispatch_game_surface_point(
            x_ratio * GAME_REFERENCE_WIDTH,
            y_ratio * GAME_REFERENCE_HEIGHT,
            input_kind="mouse",
        )

    def dispatch_game_surface_input_ratio(
        self,
        x_ratio: float,
        y_ratio: float,
        *,
        input_kind: str,
        viewport_hit_test: bool = False,
    ) -> None:
        """Compatibility wrapper accepting normalized canvas coordinates."""
        self.dispatch_game_surface_point(
            x_ratio * GAME_REFERENCE_WIDTH,
            y_ratio * GAME_REFERENCE_HEIGHT,
            input_kind=input_kind,
            viewport_hit_test=viewport_hit_test,
        )

    def dispatch_game_surface_point(
        self,
        reference_x: float,
        reference_y: float,
        *,
        input_kind: str,
        viewport_hit_test: bool = False,
    ) -> None:
        """Dispatch input from the one canonical 1280x720 canvas origin.

        A fresh canvas measurement supplies only the live surface dimensions;
        iframe/page offsets are deliberately discarded because CSS co-locates
        the game surface with the renderer origin. Mouse and touch both use
        the same page-level CDP dispatcher, including X/Y overlay focus. This
        works for OOPIF canvases, never moves the OS cursor, and never changes
        window z-order.
        """
        point = CanvasReferencePoint(float(reference_x), float(reference_y))
        if input_kind not in {"mouse", "touch"}:
            raise ValueError(f"Game input kind không được hỗ trợ: {input_kind}")
        _canvas, transform = self._game_surface_transform_snapshot()
        viewport_x, viewport_y = transform.to_viewport(point)
        cdp = self._get_page_cdp_session(self.page)
        ProfileInputEngine.click(
            cdp,
            ViewportPoint(viewport_x, viewport_y),
            kind=input_kind,
        )

    def dispatch_game_surface_profile_mouse_point(
        self, reference_x: float, reference_y: float
    ) -> dict[str, float | str]:
        """Click one canvas point through Playwright's iframe-aware mouse.

        Mail controls can be hosted by a game iframe in a separate renderer.
        Raw page-target CDP mouse events acknowledge successfully but may not
        be hit-tested into that frame.  This remains synthetic, profile-local
        input: it never moves the Windows cursor or activates the window.
        """
        point = CanvasReferencePoint(float(reference_x), float(reference_y))
        # Playwright's page.mouse uses main-page viewport coordinates.  Its
        # transform must retain the shared iframe/canvas position in that
        # viewport; this is distinct from CDP game input, whose canonical
        # canvas origin deliberately has no iframe offset.
        _canvas, transform = self._game_surface_page_mouse_transform_snapshot()
        viewport_x, viewport_y = transform.to_viewport(point)
        viewport_point = ViewportPoint(viewport_x, viewport_y)
        ProfileInputEngine.mirrored_pointer(
            self.page, viewport_point, event_type="pointerdown"
        )
        ProfileInputEngine.mirrored_pointer(
            self.page, viewport_point, event_type="pointerup"
        )
        return {
            "dispatch_route": "playwright_page_mouse",
            "reference_x": round(point.x, 3),
            "reference_y": round(point.y, 3),
            "viewport_x": round(viewport_x, 3),
            "viewport_y": round(viewport_y, 3),
            "surface_x": round(transform.viewport_left, 3),
            "surface_y": round(transform.viewport_top, 3),
            "surface_width": round(transform.css_width, 3),
            "surface_height": round(transform.css_height, 3),
        }

    def _game_surface_page_mouse_transform_snapshot(
        self,
    ) -> tuple[Any | None, CanvasTransformSnapshot]:
        """Map canonical game points to the main-page mouse viewport.

        ``page.mouse`` is iframe-aware, but it still accepts coordinates from
        the main page.  Preserve the game surface's main-page left/top here.
        This is not a canvas-to-iframe adjustment: the two surfaces share one
        origin; it is the required final conversion from that shared origin to
        the page viewport.
        """
        canvas, surface = self._canvas_or_viewport()
        box: dict[str, float] | None = None
        if canvas is not None:
            bounding_box = getattr(canvas, "bounding_box", None)
            if callable(bounding_box):
                try:
                    measured = bounding_box()
                    if (
                        isinstance(measured, dict)
                        and float(measured.get("width", 0)) > 0
                        and float(measured.get("height", 0)) > 0
                    ):
                        box = {
                            "x": float(measured.get("x", 0.0)),
                            "y": float(measured.get("y", 0.0)),
                            "width": float(measured["width"]),
                            "height": float(measured["height"]),
                        }
                except Exception:
                    box = None
        if box is None:
            frame = self.find_frame()
            if frame != self.page.main_frame:
                try:
                    measured = frame.frame_element().bounding_box()
                    if measured is not None:
                        box = {
                            "x": float(measured.get("x", 0.0)),
                            "y": float(measured.get("y", 0.0)),
                            "width": float(measured["width"]),
                            "height": float(measured["height"]),
                        }
                except Exception:
                    box = None
        # The renderer screenshot is always the logical 1280×720 image, but
        # the page can display that image in a smaller CSS canvas (for example
        # ~720×405 in the supplied recording). page.mouse consumes CSS-page
        # coordinates, so it must map the logical point through this measured
        # box exactly once. Treating logical pixels as CSS pixels sent the
        # click down/right of every mailbox control.
        if box is not None:
            return canvas, CanvasTransformSnapshot.from_box(box)
        return canvas, CanvasTransformSnapshot.from_box(surface)

    def _game_surface_transform_snapshot(
        self,
    ) -> tuple[Any | None, CanvasTransformSnapshot]:
        """Measure once and return the sole mapping from 1280x720 to live canvas."""
        canvas, surface = self._canvas_or_viewport()
        # A renderer lease has already resized the game frame to the canonical
        # coordinate system.  Do not read the canvas CSS box here: Chromium
        # can report a compositor-scaled box while the lease is active, which
        # would scale a 1280x720 point a second time and shift it left/up.
        if getattr(self, "_automation_game_frame_fixed", False):
            return canvas, CanvasTransformSnapshot.from_box(_fixed_game_surface_box())
        canvas_box: dict[str, float] | None = None
        if canvas is not None:
            bounding_box = getattr(canvas, "bounding_box", None)
            if callable(bounding_box):
                try:
                    measured = bounding_box()
                    if (
                        isinstance(measured, dict)
                        and float(measured.get("width", 0)) > 0
                        and float(measured.get("height", 0)) > 0
                    ):
                        canvas_box = {
                            "x": float(measured.get("x", 0)),
                            "y": float(measured.get("y", 0)),
                            "width": float(measured["width"]),
                            "height": float(measured["height"]),
                        }
                except Exception:
                    canvas_box = None
        point_surface = canvas_box or {
            "x": 0.0,
            "y": 0.0,
            "width": float(surface["width"]),
            "height": float(surface["height"]),
        }
        # Input has one origin: the top-left of the game surface. The portal
        # and automation CSS make the iframe/canvas co-located, so carrying a
        # locator's page offset into CDP would reintroduce the historical 8px
        # (or host-layout) drift. Keep only the freshly measured dimensions.
        return canvas, CanvasTransformSnapshot.from_box(_origin_surface_box(point_surface))

    def set_sync_source(self, enabled: bool) -> SyncSourceStatus:
        enabled = bool(enabled)
        if self._sync_source == enabled:
            # Followers already start with capture disabled. Reconfiguring all
            # of their frames when Sync is enabled can block a slow profile's
            # worker long enough for its input commands to pile up behind it.
            # A source is different: its iframe document may have been
            # replaced while the Python-side flag remained True. Reinstall a
            # missing probe instead of repeatedly observing zero armed frames.
            if enabled:
                legacy_repair = self.__dict__.get("_repair_and_count_sync_frames")
                if callable(legacy_repair):
                    armed = int(legacy_repair())
                    return SyncSourceStatus(armed, armed, armed, bool(armed))
                return self._repair_sync_source()
            return SyncSourceStatus()
        self._sync_source = enabled
        if enabled:
            # Sync needs only the event probe. Running the full canvas/style
            # configuration here delayed arming the master for many seconds
            # on a retained profile, so early clicks were silently lost.
            return self._repair_sync_source()
        # Never deep-walk FrameLocators while disabling: stale nested frames
        # can consume Playwright's 60-second timeout and strand the next
        # enable request in the worker queue.
        roots: list[Any] = list(getattr(self, "_sync_capture_roots", ()))
        with contextlib.suppress(Exception):
            roots.extend(self.page.frames)
        seen: set[int] = set()
        for frame in roots:
            if id(frame) in seen:
                continue
            seen.add(id(frame))
            try:
                self._evaluate_frame_root(
                    frame,
                    """() => {
                        window.__IK_SYNC_SOURCE = false;
                        window.__IK_SET_INTERACTION_MODES?.(false, false);
                    }"""
                )
            except Exception:
                continue
        self._sync_capture_roots = []
        self._sync_active_roots = []
        return SyncSourceStatus()

    def repair_synced_input_runtime(self) -> None:
        """Reconnect input dispatch after a follower frame/page navigation."""
        page = self.page
        self._sync_pointer_target_box = None
        self._sync_last_target_box = None
        self._detach_page_cdp_session()
        self._runtime_page = None
        self._configured_frames.clear()
        self._ensure_page_runtime(page)
        self._configure_interaction_frames(force=True)

    def _discover_sync_input_roots(self) -> tuple[list[Any], int, int]:
        """Return every accessible document root, not only canvas documents.

        A game can receive input through a WebGL host, portal document, or a
        nested cross-origin iframe whose canvas is not inspectable.  Canvas is
        therefore diagnostics only; successful probe installation is the
        activation contract.
        """
        direct: list[Any] = []
        nested: list[Any] = []
        with contextlib.suppress(Exception):
            direct = list(reversed(self.page.frames))
        with contextlib.suppress(Exception):
            nested = list(self._frame_roots())
        roots = [*direct, *nested]
        candidates: list[Any] = []
        seen: set[int] = set()
        for root in roots:
            if id(root) in seen:
                continue
            seen.add(id(root))
            candidates.append(root)
        return candidates, len(direct), len(nested)

    def _repair_sync_source(self) -> SyncSourceStatus:
        """Discover, arm and verify only real game/input roots."""
        started_at = time.monotonic()
        candidates, page_frame_count, nested_root_count = self._discover_sync_input_roots()
        self._sync_source_diagnostics: list[dict[str, Any]] = [
            {
                "event": "sync_source_discovery", "page_frame_count": page_frame_count,
                "nested_root_count": nested_root_count, "candidate_root_count": len(candidates),
            }
        ]
        verified: list[Any] = []
        armed = 0
        for root in candidates:
            safe_url = redact_url(self._automation_root_url(root) or "")
            kind = "FrameLocator" if not callable(getattr(root, "evaluate", None)) else "Frame"
            visible = self._visible_canvas(root)
            try:
                self._evaluate_frame_root(root, INTERACTION_PROBE)
                ready = bool(self._evaluate_frame_root(
                    root,
                    """([syncSource, inspectEnabled]) => {
                        if (!Array.isArray(window.__IK_SYNC_EVENTS)) window.__IK_SYNC_EVENTS = [];
                        if (!Array.isArray(window.__IK_COORDINATE_EVENTS)) window.__IK_COORDINATE_EVENTS = [];
                        window.__IK_SET_INTERACTION_MODES?.(syncSource, inspectEnabled);
                        return Boolean(window.__IK_INTERACTION_PROBE_INSTALLED &&
                            Array.isArray(window.__IK_SYNC_EVENTS) &&
                            typeof window.__IK_SET_INTERACTION_MODES === 'function' &&
                            window.__IK_SYNC_SOURCE === Boolean(syncSource));
                    }""",
                    [self._sync_source, self._inspector_enabled],
                ))
                self._sync_source_diagnostics.append({"event": "sync_source_candidate",
                    "frame_url_safe": safe_url, "root_kind": kind,
                    "has_visible_canvas": visible is not None,
                    "probe_installed": ready,
                })
                if ready:
                    armed += 1
                    verified.append(root)
                    self._configured_frames[id(root)] = (
                        f"{self._automation_root_url(root) or ''}|{self._sync_source}|{self._inspector_enabled}|"
                        f"{self._drag_item_visible}|{self._scrollbars_visible}"
                    )
                    self._sync_source_diagnostics.append({"event": "sync_source_verified_root", "frame_url_safe": safe_url})
            except Exception as error:
                self._configured_frames.pop(id(root), None)
                self._sync_source_diagnostics.append({"event": "sync_source_candidate",
                    "frame_url_safe": safe_url, "root_kind": kind,
                    "has_visible_canvas": visible is not None,
                    "probe_installed": False, "error": type(error).__name__,
                })
        self._sync_capture_roots = verified
        self._sync_active_roots = [
            root for root in getattr(self, "_sync_active_roots", ())
            if any(root is armed_root for armed_root in verified)
        ]
        status = SyncSourceStatus(
            armed_frame_count=armed,
            input_frame_count=len(verified),
            game_frame_count=len(candidates),
            ready=bool(verified),
            failure_reason="" if verified else "Không arm được bất kỳ document root nào cho Sync",
        )
        self._sync_source_last_status = status
        self._sync_source_diagnostics.append({"event": "sync_source_ready" if status.ready else "sync_source_failed",
            "root_count": len(candidates), "verified_input_root_count": len(verified),
            "armed_frame_count": armed, "duration_ms": round((time.monotonic() - started_at) * 1000),
            "failure_reason": status.failure_reason,
        })
        return status

    def sync_source_diagnostics(self) -> tuple[dict[str, Any], ...]:
        return tuple(getattr(self, "_sync_source_diagnostics", ()))

    def _repair_and_count_sync_frames(self) -> int:
        """Compatibility shim; readiness decisions must use status instead."""
        status = self._repair_sync_source()
        if status.armed_frame_count:
            return status.armed_frame_count
        # Kept only for legacy callers/tests that used this private integer
        # helper. The real Sync lifecycle calls ``_repair_sync_source`` and
        # never accepts this fallback as readiness evidence.
        armed = 0
        for root in getattr(self.page, "frames", ()):
            try:
                self._evaluate_frame_root(root, INTERACTION_PROBE)
                ready = self._evaluate_frame_root(
                    root,
                    "() => Boolean(window.__IK_INTERACTION_PROBE_INSTALLED)",
                )
                # Legacy private-helper contract also applied the source mode.
                # It is not used to determine the modern source ACK.
                self._evaluate_frame_root(
                    root,
                    "([syncSource, inspectEnabled]) => window.__IK_SET_INTERACTION_MODES?.(syncSource, inspectEnabled)",
                    [self._sync_source, self._inspector_enabled],
                )
                if ready:
                    armed += 1
                    self._configured_frames[id(root)] = (
                        f"{self._automation_root_url(root) or ''}|{self._sync_source}|{self._inspector_enabled}|"
                        f"{self._drag_item_visible}|{self._scrollbars_visible}"
                    )
            except Exception:
                continue
        return armed

    def sync_capture_frame_count(self) -> int:
        """Return the number of frames whose input probe is actively armed."""
        if not self._sync_source:
            return 0
        armed = 0
        for frame in getattr(self, "_sync_capture_roots", ()):
            try:
                if self._evaluate_frame_root(
                    frame,
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

    def sync_source_status(self) -> SyncSourceStatus:
        """Cheap health check over cached verified roots only."""
        if not self._sync_source:
            return SyncSourceStatus()
        valid = 0
        stale = False
        for root in list(getattr(self, "_sync_capture_roots", ())):
            try:
                ready = self._evaluate_frame_root(root, """() => Boolean(
                    window.__IK_INTERACTION_PROBE_INSTALLED &&
                    Array.isArray(window.__IK_SYNC_EVENTS) && window.__IK_SYNC_SOURCE === true
                )""")
                if ready:
                    valid += 1
                else:
                    stale = True
            except Exception:
                stale = True
        status = SyncSourceStatus(valid, valid, valid, bool(valid),
            "Armed Sync root disappeared" if stale and not valid else "")
        self._sync_source_last_status = status
        return status

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
        events: list[dict[str, Any]] = []
        if not self._sync_source:
            return events
        valid_roots: list[Any] = []
        for frame in tuple(getattr(self, "_sync_capture_roots", ())):
            try:
                rows = self._evaluate_frame_root(
                    frame, "() => window.__IK_SYNC_EVENTS?.splice(0) || []"
                )
            except Exception:
                # Do not silently keep a detached iframe forever. Periodic
                # source health will rediscover it (or safely disable Sync).
                continue
            valid_roots.append(frame)
            for row in rows:
                frame_url = self._automation_root_url(frame) or ""
                row["frame_url"] = frame_url
                row["frame_url_safe"] = redact_url(frame_url)
                if str(row.get("type", "")) in {"pointerdown", "pointerup", "wheel", "keydown", "keyup"}:
                    active = list(getattr(self, "_sync_active_roots", ()))
                    if not any(frame is root for root in active):
                        active.append(frame)
                        self._sync_active_roots = active
                events.append(row)
        self._sync_capture_roots = valid_roots
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

    def apply_synced_input(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Apply one mirrored event and return its resolved pointer transform."""
        event_type = str(event.get("type", ""))
        if event_type in {"keydown", "keyup"}:
            self._apply_synced_keyboard_input(event, event_type)
            return None
        box = self._synced_pointer_target_box(event)
        x, y = calculate_target_point(event, box)
        button_number = int(event.get("pointer", {}).get("button", 0))
        button = {0: "left", 1: "middle", 2: "right"}.get(button_number, "left")
        point = ViewportPoint(x, y)
        if event_type in {"pointerdown", "pointermove", "pointerup"}:
            ProfileInputEngine.mirrored_pointer(
                self.page,
                point,
                event_type=event_type,
                button=button,
            )
            if event_type == "pointerdown":
                buttons = getattr(self, "_sync_pressed_buttons", None)
                if buttons is None:
                    buttons = self._sync_pressed_buttons = set()
                buttons.add(button)
            if event_type == "pointerup":
                getattr(self, "_sync_pressed_buttons", set()).discard(button)
                self._sync_pointer_target_box = None
        elif event_type == "wheel":
            wheel = event.get("wheel", {})
            ProfileInputEngine.mirrored_wheel(
                self.page,
                point,
                float(wheel.get("delta_x", 0.0)),
                float(wheel.get("delta_y", 0.0)),
            )
        return {
            # Keep this in the worker event log.  A successful raw CDP call is
            # not sufficient evidence that an OOPIF game surface received a
            # pointer event; mirrored input must go through Playwright's page
            # mouse so Chromium performs the frame-aware hit test.
            "dispatch_route": "playwright_page_mouse",
            "x": round(float(x), 3),
            "y": round(float(y), 3),
            "surface_x": round(float(box.get("x", 0.0)), 3),
            "surface_y": round(float(box.get("y", 0.0)), 3),
            "surface_width": round(float(box["width"]), 3),
            "surface_height": round(float(box["height"]), 3),
        }

    def _synced_pointer_target_box(self, event: dict[str, Any]) -> dict[str, float]:
        """Resolve and retain one canvas-local transform for a complete gesture.

        During iframe navigation Chrome can visibly retain the game frame
        while Playwright temporarily cannot enumerate its canvas. Reuse the
        last measured size before falling back to the viewport so a temporary
        detached frame does not change a gesture halfway through.
        """
        event_type = str(event.get("type", ""))
        active_box = getattr(self, "_sync_pointer_target_box", None)
        if event_type in {"pointermove", "pointerup"} and active_box is not None:
            return dict(active_box)
        try:
            frame = self._frame_for_input(event)
            canvas = event.get("canvas")
            if isinstance(canvas, dict):
                box = self._canvas_box(frame, int(canvas.get("index", 0)))
            else:
                box = self._frame_box(frame)
        except Exception:
            cached = getattr(self, "_sync_last_target_box", None)
            box = dict(cached) if cached is not None else self._viewport_surface_box()
        # Enforce the shared game origin at the final sync boundary as well.
        # This protects the dispatcher if a future locator/frame fallback
        # starts returning page-relative x/y again.
        box = _origin_surface_box(box)
        self._sync_last_target_box = dict(box)
        if event_type == "pointerdown":
            self._sync_pointer_target_box = dict(box)
        return box

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
        ProfileInputEngine.key_event(self._get_page_cdp_session(self.page), params)
        identity = code or key
        pressed_keys = getattr(self, "_sync_pressed_keys", None)
        if pressed_keys is None:
            pressed_keys = self._sync_pressed_keys = {}
        if event_type == "keydown":
            pressed_keys[identity] = dict(params)
        else:
            pressed_keys.pop(identity, None)

    def reset_synced_input(self) -> None:
        """Release mirrored held keys/buttons after an aborted Sync session.

        This only runs on the owning ProfileWorker thread.  It is deliberately
        best-effort: a follower that already died cannot receive input, while
        healthy followers must never be left dragging or holding a modifier.
        """
        page = self._page
        if page is not None and not page.is_closed():
            for button in tuple(getattr(self, "_sync_pressed_buttons", ())):
                with contextlib.suppress(Exception):
                    page.mouse.up(button=button)
        getattr(self, "_sync_pressed_buttons", set()).clear()
        self._sync_pointer_target_box = None
        try:
            cdp = self._get_page_cdp_session(self.page)
            for params in tuple(getattr(self, "_sync_pressed_keys", {}).values()):
                released = dict(params)
                released.pop("text", None)
                released.pop("unmodifiedText", None)
                released["type"] = "keyUp"
                released["autoRepeat"] = False
                try:
                    ProfileInputEngine.key_event(cdp, released)
                except Exception:
                    continue
        except Exception:
            pass
        getattr(self, "_sync_pressed_keys", {}).clear()

    def _configure_interaction_frames(self, *, force: bool = False) -> None:
        if self._page is None or self._page.is_closed():
            return
        active_keys: set[int] = set()
        for frame in self._frame_roots():
            key = id(frame)
            active_keys.add(key)
            signature = (
                f"{self._automation_root_url(frame) or ''}|{self._sync_source}|{self._inspector_enabled}|"
                f"{self._drag_item_visible}|{self._scrollbars_visible}"
            )
            if not force and self._configured_frames.get(key) == signature:
                continue
            try:
                self._evaluate_frame_root(frame, GAME_FRAME_FIT_SCRIPT)
                if getattr(self, "_automation_game_frame_fixed", False):
                    self._evaluate_frame_root(
                        frame,
                        GAME_FRAME_SIZE_SCRIPT,
                        [True, int(_GAME_SURFACE_WIDTH), int(_GAME_SURFACE_HEIGHT)],
                    )
                self._evaluate_frame_root(frame, INTERACTION_PROBE)
                self._evaluate_frame_root(
                    frame,
                    "([syncSource, inspectEnabled]) => "
                    "window.__IK_SET_INTERACTION_MODES?.(syncSource, inspectEnabled)",
                    [self._sync_source, self._inspector_enabled],
                )
                self._evaluate_frame_root(
                    frame,
                    "visible => window.__IK_SET_DRAG_ITEM_VISIBLE?.(visible)",
                    self._drag_item_visible,
                )
                self._evaluate_frame_root(
                    frame,
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
                self._evaluate_frame_root(
                    frame,
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
            largest = max(boxes, key=lambda item: item["width"] * item["height"])
            if getattr(self, "_automation_game_frame_fixed", False):
                return _fixed_game_surface_box()
            # Every game document is CSS-fitted to the renderer origin. Keep
            # only its live dimensions: carrying the locator's x/y into CDP
            # gives every profile a different origin in a large window grid.
            return _origin_surface_box(largest)
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
            if getattr(self, "_automation_game_frame_fixed", False):
                return _fixed_game_surface_box()
            return _origin_surface_box(box)
        raise RuntimeError("Không xác định được vị trí frame đích để đồng bộ thao tác")

    def pump(self, milliseconds: int = 50) -> None:
        if self._page is not None and not self._page.is_closed():
            self._page.wait_for_timeout(milliseconds)
            # Retained profiles can be idle in this loop for their whole
            # lifetime. Rewriting every iframe's game CSS on each pump blocks
            # the worker for tens of seconds and delays queued Sync commands.
            # Fresh pages are configured in ``start``; later configuration is
            # needed only while an interactive/automation feature is active.
            if (
                self._sync_source
                or self._inspector_enabled
                or self._drag_item_visible
                or self._scrollbars_visible
                or getattr(self, "_automation_game_frame_fixed", False)
            ):
                self._configure_interaction_frames()
            self._retry_auto_login_if_due()

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
                with contextlib.suppress(Exception):
                    self._context.close()
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
                with contextlib.suppress(Exception):
                    self._playwright.stop()
                self._playwright = None
            self._closing = False
