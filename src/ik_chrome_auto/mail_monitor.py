"""Small, region-scoped recognizer for the in-game combat mailbox."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAIL_BASELINE = "mail_baseline"
NO_NEW_COMBAT_MAIL = "no_new_combat_mail"
COMBAT_MAIL_OTHER = "combat_mail_other"
TERRITORY_ATTACKED = "territory_attacked"
SCAN_ERROR = "scan_error"
# This non-alerting outcome lets the dashboard clear a scan that was cancelled
# before its worker opened the mailbox.
SCAN_CANCELLED = "scan_cancelled"


@dataclass(frozen=True, slots=True)
class MailMatch:
    bounds: tuple[int, int, int, int]
    confidence: float


@dataclass(frozen=True, slots=True)
class _TemplateSpec:
    filename: str
    reference_size: tuple[int, int]
    region: tuple[float, float, float, float]
    threshold: float
    edge: bool = True


_MAIL_CLOSE_SPECS = (
    _TemplateSpec(
        "mail_close.png",
        (1920, 1080),
        (0.86, 0.01, 0.995, 0.22),
        0.55,
    ),
    # The reference video has a slightly wider renderer and a smaller X.
    # Keeping a second tight crop is more reliable than lowering the gate and
    # accidentally accepting white HUD text in the same corner.
    _TemplateSpec(
        "mail_close_video.png",
        (1260, 674),
        (0.86, 0.01, 0.995, 0.22),
        0.62,
    ),
)
_TERRITORY_ATTACKED = _TemplateSpec(
    "territory_attacked.png",
    (1920, 1080),
    # Only the subject line inside the first list row. The second row and the
    # detail body are excluded so an older attack mail cannot authorise an
    # alert for a different first message.
    (0.14, 0.12, 0.40, 0.25),
    0.58,
    edge=False,
)
_COMBAT_UNREAD_ONE = _TemplateSpec(
    "combat_unread_one.png",
    (1920, 1080),
    # Only the badge attached to the left-side Combat category is valid.
    (0.068, 0.25, 0.135, 0.36),
    # Real full-renderer badge-1 samples score ~0.46 while a badge-15 sample
    # from the reference video scores ~0.20. Keep the gate between them; the
    # tight Combat-only ROI remains the primary false-positive boundary.
    0.38,
)


class BrowserMailMonitor:
    """Recognize only controls and text needed by the mailbox state machine."""

    def __init__(self, asset_dir: Path | None = None) -> None:
        self.asset_dir = asset_dir or Path(__file__).with_name("assets") / "mail_monitor"
        self._templates: dict[str, object] = {}

    def find_close_button(self, screenshot_png: bytes) -> MailMatch | None:
        matches = [self._find(screenshot_png, spec) for spec in _MAIL_CLOSE_SPECS]
        found = [match for match in matches if match is not None]
        return max(found, key=lambda match: match.confidence) if found else None

    def is_mail_open(self, screenshot_png: bytes) -> bool:
        return self.find_close_button(screenshot_png) is not None

    def has_new_combat_mail(self, screenshot_png: bytes) -> bool:
        """Accept only a red badge whose displayed unread count is exactly 1."""
        return self._find(screenshot_png, _COMBAT_UNREAD_ONE) is not None

    def is_territory_attacked(self, screenshot_png: bytes) -> bool:
        return self._find(screenshot_png, _TERRITORY_ATTACKED) is not None

    def _find(self, screenshot_png: bytes, spec: _TemplateSpec) -> MailMatch | None:
        import cv2

        image = self._decode(screenshot_png)
        height, width = image.shape[:2]
        left = round(width * spec.region[0])
        top = round(height * spec.region[1])
        right = round(width * spec.region[2])
        bottom = round(height * spec.region[3])
        search = image[top:bottom, left:right]
        template = self._load(spec.filename)
        base_width = max(8, round(template.shape[1] * width / spec.reference_size[0]))
        base_height = max(8, round(template.shape[0] * height / spec.reference_size[1]))
        if spec.edge:
            match_search = cv2.Canny(cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), 45, 145)
        else:
            match_search = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
        best_score = 0.0
        best_bounds: tuple[int, int, int, int] | None = None
        for variant in (0.86, 0.93, 1.0, 1.07, 1.14):
            target_width = max(8, round(base_width * variant))
            target_height = max(8, round(base_height * variant))
            if target_width > search.shape[1] or target_height > search.shape[0]:
                continue
            scaled = cv2.resize(template, (target_width, target_height), interpolation=cv2.INTER_AREA)
            if spec.edge:
                match_template = cv2.Canny(cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY), 45, 145)
            else:
                match_template = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
            result = cv2.matchTemplate(match_search, match_template, cv2.TM_CCOEFF_NORMED)
            _minimum, maximum, _minimum_at, maximum_at = cv2.minMaxLoc(result)
            if float(maximum) > best_score:
                best_score = float(maximum)
                x, y = maximum_at
                best_bounds = (
                    left + x,
                    top + y,
                    target_width,
                    target_height,
                )
        if best_bounds is None or best_score < spec.threshold:
            return None
        return MailMatch(best_bounds, best_score)

    @staticmethod
    def _decode(screenshot_png: bytes):
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(screenshot_png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise ValueError("Ảnh giám sát hộp thư không hợp lệ")
        return image

    def _load(self, filename: str):
        import cv2

        cached = self._templates.get(filename)
        if cached is not None:
            return cached
        path = self.asset_dir / filename
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(f"Thiếu template giám sát thư: {path}")
        self._templates[filename] = image
        return image
