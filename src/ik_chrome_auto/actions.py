from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from ik_chrome_auto.browser import ChromeProfileSession
from ik_chrome_auto.storage import prune_profile_images, upscale_png_for_diagnostics, write_retained_png

StatusCallback = Callable[[str], None]

class ActionCancelled(RuntimeError):
    """Raised when the user stops an automation."""

class AutomationFunctions:
    """Public automation API. All calls run on the owning profile worker thread."""

    def __init__(
        self,
        session: ChromeProfileSession,
        stop_event: threading.Event,
        status: StatusCallback,
    ) -> None:
        self.session = session
        self.stop_event = stop_event
        self.status = status

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise ActionCancelled

    def goto(self, url: str | None = None) -> None:
        self._check_cancelled()
        self.status("Mở trang game")
        self.session.goto(url)

    def wait_game_ready(self, timeout_ms: int = 90_000) -> None:
        self.status("Chờ iframe/canvas game sẵn sàng")
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            self._check_cancelled()
            for frame in reversed(self.session.page.frames):
                if frame == self.session.page.main_frame:
                    continue
                try:
                    ready = frame.evaluate(
                        """
                        () => document.readyState === 'complete' &&
                          (document.querySelectorAll('canvas').length > 0 ||
                           (document.body && document.body.children.length > 0))
                        """
                    )
                    if ready and ("gtarcade.com" in frame.url or frame.url != "about:blank"):
                        self.status("Game đã sẵn sàng")
                        return
                except Exception:
                    continue
            self.session.pump(300)
        raise TimeoutError(f"Game chưa sẵn sàng sau {timeout_ms / 1000:.0f} giây")

    def wait_network_idle(self, timeout_ms: int = 15_000) -> None:
        self._check_cancelled()
        self.status("Chờ network idle")
        self.session.page.wait_for_load_state("networkidle", timeout=timeout_ms)

    def wait_for_text(
        self,
        text: str,
        *,
        frame_url_contains: str | None = None,
        timeout_ms: int = 30_000,
        exact: bool = False,
    ) -> None:
        self._check_cancelled()
        self.status(f"Chờ text: {text}")
        frame = self.session.find_frame(frame_url_contains)
        frame.get_by_text(text, exact=exact).first.wait_for(state="visible", timeout=timeout_ms)

    def click(
        self,
        selector: str,
        *,
        frame_url_contains: str | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        self._check_cancelled()
        self.status(f"Click selector: {selector}")
        frame = self.session.find_frame(frame_url_contains)
        frame.locator(selector).first.click(timeout=timeout_ms)

    def click_text(
        self,
        text: str,
        *,
        frame_url_contains: str | None = None,
        exact: bool = False,
        timeout_ms: int = 30_000,
    ) -> None:
        self._check_cancelled()
        self.status(f"Click text: {text}")
        frame = self.session.find_frame(frame_url_contains)
        frame.get_by_text(text, exact=exact).first.click(timeout=timeout_ms)

    def click_ratio(
        self,
        x: float,
        y: float,
        *,
        target: str = "canvas",
        frame_url_contains: str | None = None,
    ) -> None:
        self._check_cancelled()
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError("click_ratio yêu cầu x và y trong khoảng 0..1")
        box = self.session.surface_box(target=target, frame_url_contains=frame_url_contains)
        click_x = box["width"] * x
        click_y = box["height"] * y
        self.status(f"Click {target} tại tỉ lệ ({x:.3f}, {y:.3f})")
        self.session.page.mouse.click(click_x, click_y)

    def type_text(
        self,
        selector: str,
        text: str,
        *,
        frame_url_contains: str | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        self._check_cancelled()
        self.status(f"Điền input: {selector} (nội dung không ghi log)")
        frame = self.session.find_frame(frame_url_contains)
        frame.locator(selector).first.fill(text, timeout=timeout_ms)

    def press(self, key: str) -> None:
        self._check_cancelled()
        self.status(f"Gửi phím: {key}")
        self.session.page.keyboard.press(key)

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds không được âm")
        self.status(f"Chờ {seconds:.1f} giây")
        if self.stop_event.wait(seconds):
            raise ActionCancelled
        self.session.pump(10)

    def read_state(self) -> Path:
        self._check_cancelled()
        self.status("Đọc portal, frame, canvas và message")
        _, path = self.session.reader.snapshot(self.session.page)
        self.status(f"Đã lưu snapshot: {path.name}")
        return path

    def screenshot(self, name: str = "screen", *, full_page: bool = False) -> Path:
        self._check_cancelled()
        safe_name = "".join(char for char in name if char.isalnum() or char in "-_") or "screen"
        folder = self.session.config.data_dir / "screenshots" / self.session.profile.id
        folder.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = folder / f"{timestamp}-{safe_name}.png"
        self.status(f"Chụp ảnh: {path.name}")
        if full_page:
            self.session.page.screenshot(path=str(path), full_page=True)
            prune_profile_images(folder, keep=2)
        else:
            png, _surface = self.session.capture_game_surface_png()
            write_retained_png(path, upscale_png_for_diagnostics(png), keep=2)
        return path
