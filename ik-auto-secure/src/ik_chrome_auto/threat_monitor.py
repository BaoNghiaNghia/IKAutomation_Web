"""Lightweight, region-scoped threat recognition for browser profiles."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


INVESTIGATED = "investigated"
INCOMING_ATTACK = "incoming_attack"


@dataclass(frozen=True, slots=True)
class ThreatMatch:
    event: str
    confidence: float


@dataclass(frozen=True, slots=True)
class _ThreatSpec:
    event: str
    filename: str
    threshold: float
    # Normalized search region: left, top, right, bottom.
    region: tuple[float, float, float, float]
    edge: bool = False


_REFERENCE_SIZE = (2560, 1182)
_SPECS = (
    _ThreatSpec(
        INVESTIGATED,
        "investigated.png",
        # The attack banner has a similar pale label elsewhere in the same
        # area and scored ~0.66 in a negative sample. Keep this title gate
        # deliberately higher; verified browser-size samples score >0.95.
        0.78,
        (0.04, 0.43, 0.43, 0.82),
    ),
    _ThreatSpec(
        INCOMING_ATTACK,
        "incoming_attack_prefix.png",
        0.52,
        (0.18, 0.28, 0.82, 0.68),
        edge=True,
    ),
)


class BrowserThreatMonitor:
    """Detect only the two user-approved warnings in targeted canvas ROIs."""

    def __init__(self, asset_dir: Path | None = None) -> None:
        self.asset_dir = asset_dir or Path(__file__).with_name("assets") / "threats"
        self._templates: dict[str, object] = {}

    def detect(self, screenshot_png: bytes) -> tuple[ThreatMatch, ...]:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(screenshot_png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise ValueError("Ảnh giám sát không hợp lệ")
        height, width = image.shape[:2]
        matches: list[ThreatMatch] = []
        for spec in _SPECS:
            left = max(0, min(width - 1, round(width * spec.region[0])))
            top = max(0, min(height - 1, round(height * spec.region[1])))
            right = max(left + 1, min(width, round(width * spec.region[2])))
            bottom = max(top + 1, min(height, round(height * spec.region[3])))
            search = image[top:bottom, left:right]
            score = self._best_score(search, width, height, spec)
            if score >= spec.threshold:
                matches.append(ThreatMatch(spec.event, score))
        return tuple(matches)

    def region(self, event: str) -> tuple[float, float, float, float]:
        return self._spec(event).region

    def detect_region(
        self,
        screenshot_png: bytes,
        event: str,
        full_canvas_size: tuple[int, int],
        capture_scale: float,
    ) -> ThreatMatch | None:
        """Match one event inside an already cropped renderer capture."""
        import cv2
        import numpy as np

        search = cv2.imdecode(
            np.frombuffer(screenshot_png, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if search is None or search.size == 0:
            raise ValueError("Ảnh crop giám sát không hợp lệ")
        spec = self._spec(event)
        scaled_width = max(1, round(full_canvas_size[0] * capture_scale))
        scaled_height = max(1, round(full_canvas_size[1] * capture_scale))
        score = self._best_score(search, scaled_width, scaled_height, spec)
        if score < spec.threshold:
            return None
        return ThreatMatch(event, score)

    def _best_score(
        self, search, image_width: int, image_height: int, spec: _ThreatSpec
    ) -> float:
        import cv2

        template = self._load_template(spec.filename)
        base_width = max(8, round(template.shape[1] * image_width / _REFERENCE_SIZE[0]))
        base_height = max(6, round(template.shape[0] * image_height / _REFERENCE_SIZE[1]))
        if spec.edge:
            match_search = cv2.Canny(cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), 55, 150)
        else:
            match_search = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
        best = 0.0
        for variant in (0.88, 0.94, 1.0, 1.06, 1.12):
            target_width = max(8, round(base_width * variant))
            target_height = max(6, round(base_height * variant))
            if target_width > search.shape[1] or target_height > search.shape[0]:
                continue
            scaled = cv2.resize(template, (target_width, target_height), interpolation=cv2.INTER_AREA)
            if spec.edge:
                match_template = cv2.Canny(cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY), 55, 150)
            else:
                match_template = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
            result = cv2.matchTemplate(match_search, match_template, cv2.TM_CCOEFF_NORMED)
            best = max(best, float(cv2.minMaxLoc(result)[1]))
        return best

    def _load_template(self, filename: str):
        import cv2

        cached = self._templates.get(filename)
        if cached is not None:
            return cached
        path = self.asset_dir / filename
        template = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if template is None:
            raise FileNotFoundError(f"Thiếu template giám sát: {path}")
        self._templates[filename] = template
        return template

    @staticmethod
    def _spec(event: str) -> _ThreatSpec:
        for spec in _SPECS:
            if spec.event == event:
                return spec
        raise KeyError(f"Không có cấu hình giám sát: {event}")
