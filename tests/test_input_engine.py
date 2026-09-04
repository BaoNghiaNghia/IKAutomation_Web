from __future__ import annotations

import pytest

from ik_chrome_auto.input_engine import ProfileInputEngine, ViewportPoint


class RecordingCDP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send(self, method: str, params: dict[str, object]) -> None:
        self.calls.append((method, params))


def test_mouse_click_reuses_one_immutable_point_for_complete_gesture() -> None:
    cdp = RecordingCDP()

    ProfileInputEngine.click(cdp, ViewportPoint(523.0, 356.0), kind="mouse")

    assert [params["type"] for _method, params in cdp.calls] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert all(
        params["x"] == 523.0 and params["y"] == 356.0
        for _method, params in cdp.calls
    )


def test_touch_click_has_balanced_start_and_end() -> None:
    cdp = RecordingCDP()

    ProfileInputEngine.click(cdp, ViewportPoint(640.0, 360.0), kind="touch")

    assert [params["type"] for _method, params in cdp.calls] == [
        "touchStart",
        "touchEnd",
    ]
    assert cdp.calls[0][1]["touchPoints"][0]["x"] == 640.0  # type: ignore[index]
    assert cdp.calls[0][1]["touchPoints"][0]["y"] == 360.0  # type: ignore[index]


def test_drag_pointer_preserves_pressed_button_mask() -> None:
    cdp = RecordingCDP()

    ProfileInputEngine.pointer(
        cdp,
        ViewportPoint(300.0, 200.0),
        event_type="pointermove",
        buttons=1,
    )

    assert cdp.calls == [
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": 300.0,
                "y": 200.0,
                "button": "none",
                "buttons": 1,
            },
        )
    ]


def test_input_engine_rejects_unknown_pointer_and_click_kinds() -> None:
    cdp = RecordingCDP()

    with pytest.raises(ValueError, match="input kind"):
        ProfileInputEngine.click(cdp, ViewportPoint(1.0, 1.0), kind="native")
    with pytest.raises(ValueError, match="Pointer event"):
        ProfileInputEngine.pointer(
            cdp,
            ViewportPoint(1.0, 1.0),
            event_type="click",
        )
