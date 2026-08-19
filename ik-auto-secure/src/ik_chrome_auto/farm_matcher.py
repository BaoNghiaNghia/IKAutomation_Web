"""OpenCV template matching over a captured browser game canvas."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ik_chrome_auto.farm_vision import BrowserGameStateDetector, DETECTION_TEMPLATES, FarmTemplateId, GameDetectionResult, TemplateEvidence

# The source pack was captured at 1280x720.  A browser canvas is frequently
# stretched independently on each axis by its host page, so scaling from only
# its width makes templates noticeably too tall (or too short) and prevents a
# valid match.  Keep both dimensions as the template's reference frame.
_REFERENCE_WIDTH = 1280
_REFERENCE_HEIGHT = 720


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    filename: str
    threshold: float = 0.84
    region: str = "all"
    reference_width: int = _REFERENCE_WIDTH
    reference_height: int = _REFERENCE_HEIGHT
    alternatives: tuple[str, ...] = ()


SPECS: dict[FarmTemplateId, TemplateSpec] = {
    FarmTemplateId.WORLD_MAP_ANCHOR: TemplateSpec("world_map_anchor.png", region="lower_left"),
    # The portal renders the same City → World Map control with different
    # artwork in its green and snowy city skins. Both browser canvas templates
    # are matched, then the strongest result is used (never a fixed click).
    FarmTemplateId.CITY_TO_WORLD_MAP_BUTTON: TemplateSpec(
        "browser_city_to_world_map_green.png",
        threshold=0.78,
        region="lower_left",
        reference_width=835,
        reference_height=432,
        alternatives=("browser_city_to_world_map_button.png",),
    ),
    FarmTemplateId.CONTINENT_MAP_TITLE: TemplateSpec("continent_map_title.png"),
    FarmTemplateId.CONTINENT_MAP_HOME_TERRITORY_ANCHOR: TemplateSpec("continent_map_home_territory_anchor.png", region="center"),
    FarmTemplateId.CONTINENT_MAP_PIN_BUTTON: TemplateSpec("continent_map_pin_button.png", region="top_left"),
    FarmTemplateId.RESOURCE_SEARCH_PANEL_ANCHOR: TemplateSpec("resource_search_panel_anchor.png", region="lower"),
    FarmTemplateId.SEARCH_BUTTON_ENABLED: TemplateSpec("search_button_enabled.png", region="lower"),
    FarmTemplateId.LEVEL_MINUS_BUTTON: TemplateSpec("level_minus_button.png", region="lower"),
    FarmTemplateId.RESOURCE_TAB_SELECTED: TemplateSpec("resource_tab_selected.png", region="lower"),
    FarmTemplateId.RESOURCE_TAB_UNSELECTED: TemplateSpec("resource_tab_unselected.png", region="lower"),
    FarmTemplateId.RESOURCE_POPUP_INFO_ANCHOR: TemplateSpec("resource_popup_info_anchor.png"),
    FarmTemplateId.RESOURCE_POPUP_IRON_TITLE: TemplateSpec("resource_popup_iron_title.png"),
    FarmTemplateId.GATHER_BUTTON_ENABLED: TemplateSpec("gather_button_enabled.png"),
    FarmTemplateId.STORAGE_LIMIT_DIALOG_ANCHOR: TemplateSpec("storage_limit_dialog_anchor.png"),
    FarmTemplateId.STORAGE_LIMIT_CANCEL_BUTTON: TemplateSpec("storage_limit_cancel_button.png"),
    FarmTemplateId.RESOURCE_EXPIRY_DIALOG_ANCHOR: TemplateSpec("resource_expiry_dialog_anchor.png"),
    FarmTemplateId.TEAM_SELECTION_PANEL_ANCHOR: TemplateSpec("team_selection_panel_anchor.png"),
    FarmTemplateId.TEAM_ADJUST_FORMATION_BUTTON: TemplateSpec("team_adjust_formation_button.png"),
    FarmTemplateId.TEAM_ACTION_BUTTON_ENABLED: TemplateSpec("team_action_button_enabled.png"),
    # The publisher mark remains visible once the browser's World Map canvas
    # has finished loading. It is only used with the post-click timing guard
    # in ProfileWorker; alone it never authorises input.
    FarmTemplateId.BROWSER_CANVAS_READY_ANCHOR: TemplateSpec(
        "browser_canvas_ready_anchor.png",
        threshold=0.80,
        region="right",
        reference_width=835,
        reference_height=432,
    ),
}


class BrowserCanvasMatcher:
    """Matches the ADB template pack after scaling it to the browser canvas."""

    def __init__(self, template_root: Path | None = None) -> None:
        self.template_root = template_root or Path(__file__).with_name("assets") / "farm_templates"
        self._templates: dict[str, object] = {}

    def detect(self, screenshot_png: bytes) -> GameDetectionResult:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(screenshot_png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Canvas screenshot không phải PNG hợp lệ")
        evidence = {template_id: self._match(image, template_id) for template_id in DETECTION_TEMPLATES}
        return BrowserGameStateDetector().detect(evidence)

    def _match(self, image: object, template_id: FarmTemplateId) -> TemplateEvidence:
        spec = SPECS.get(template_id)
        if spec is None:
            return TemplateEvidence(template_id, False)
        candidates = tuple(
            self._match_template(image, template_id, spec, filename)
            for filename in (spec.filename, *spec.alternatives)
        )
        return max(candidates, key=lambda evidence: evidence.confidence)

    def _match_template(
        self,
        image: object,
        template_id: FarmTemplateId,
        spec: TemplateSpec,
        filename: str,
    ) -> TemplateEvidence:
        import cv2

        template = self._load(filename)
        image_height, image_width = image.shape[:2]
        scaled_width = max(1, round(template.shape[1] * image_width / spec.reference_width))
        scaled_height = max(1, round(template.shape[0] * image_height / spec.reference_height))
        if scaled_width > image_width or scaled_height > image_height:
            return TemplateEvidence(template_id, False)
        scaled = cv2.resize(template, (scaled_width, scaled_height), interpolation=cv2.INTER_AREA)
        x, y, width, height = self._region(spec.region, image_width, image_height)
        search = image[y : y + height, x : x + width]
        if scaled.shape[1] > search.shape[1] or scaled.shape[0] > search.shape[0]:
            return TemplateEvidence(template_id, False)
        result = cv2.matchTemplate(search, scaled, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, location = cv2.minMaxLoc(result)
        if confidence < spec.threshold:
            return TemplateEvidence(template_id, False, float(confidence))
        return TemplateEvidence(template_id, True, float(confidence), (x + int(location[0]), y + int(location[1]), scaled_width, scaled_height))

    def _load(self, filename: str) -> object:
        import cv2

        if filename not in self._templates:
            path = self.template_root / filename
            template = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if template is None:
                raise FileNotFoundError(f"Thiếu farm template: {path}")
            self._templates[filename] = template
        return self._templates[filename]

    @staticmethod
    def _region(name: str, width: int, height: int) -> tuple[int, int, int, int]:
        if name == "lower_left": return 0, height // 2, width // 2, height - height // 2
        if name == "lower": return 0, height // 2, width, height - height // 2
        if name == "top_left": return 0, 0, width // 4, height // 5
        if name == "right": return width * 3 // 4, 0, width - (width * 3 // 4), height // 2
        if name == "center": return width // 4, height // 5, width // 2, height - (height * 2 // 5)
        return 0, 0, width, height
