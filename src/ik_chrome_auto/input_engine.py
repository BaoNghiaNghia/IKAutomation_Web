"""Canonical, profile-local CDP input dispatch.

Every point accepted here is already expressed in the target page viewport.
The browser adapter is the sole owner of the 1280x720 canvas transform; this
engine only guarantees that a complete gesture uses one CDP session and one
immutable point, without touching the native cursor or window focus.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ViewportPoint:
    x: float
    y: float


class ProfileInputEngine:
    """Dispatch complete mouse, touch, wheel and keyboard gestures via CDP."""

    @staticmethod
    def click(cdp: Any, point: ViewportPoint, *, kind: str = "mouse") -> None:
        if kind == "touch":
            touch = {
                "x": point.x,
                "y": point.y,
                "radiusX": 2,
                "radiusY": 2,
                "force": 1,
                "id": 1,
            }
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchStart", "touchPoints": [touch]},
            )
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchEnd", "touchPoints": []},
            )
            return
        if kind != "mouse":
            raise ValueError(f"Game input kind không được hỗ trợ: {kind}")
        for event_type, button in (
            ("mouseMoved", "none"),
            ("mousePressed", "left"),
            ("mouseReleased", "left"),
        ):
            params: dict[str, object] = {
                "type": event_type,
                "x": point.x,
                "y": point.y,
                "button": button,
            }
            if event_type != "mouseMoved":
                params["clickCount"] = 1
            cdp.send("Input.dispatchMouseEvent", params)

    @staticmethod
    def pointer(
        cdp: Any,
        point: ViewportPoint,
        *,
        event_type: str,
        button: str = "left",
        buttons: int = 0,
    ) -> None:
        mapped = {
            "pointerdown": "mousePressed",
            "pointermove": "mouseMoved",
            "pointerup": "mouseReleased",
        }.get(event_type)
        if mapped is None:
            raise ValueError(f"Pointer event không được hỗ trợ: {event_type}")
        params: dict[str, object] = {
            "type": mapped,
            "x": point.x,
            "y": point.y,
            "button": "none" if mapped == "mouseMoved" else button,
            "buttons": max(0, int(buttons)),
        }
        if mapped != "mouseMoved":
            params["clickCount"] = 1
        cdp.send("Input.dispatchMouseEvent", params)

    @staticmethod
    def mirrored_pointer(
        page: Any,
        point: ViewportPoint,
        *,
        event_type: str,
        button: str = "left",
    ) -> None:
        """Dispatch mirrored input through Playwright's OOPIF-aware mouse.

        This remains profile-local synthetic input and never moves the native
        Windows cursor. Playwright owns routing into the visible game iframe,
        which is more reliable than sending a raw page-target CDP event when
        Chrome hosts that iframe in a separate renderer process.
        """
        mouse = page.mouse
        if event_type == "pointerdown":
            mouse.move(point.x, point.y)
            mouse.down(button=button)
        elif event_type == "pointermove":
            mouse.move(point.x, point.y)
        elif event_type == "pointerup":
            mouse.move(point.x, point.y)
            mouse.up(button=button)
        else:
            raise ValueError(f"Pointer event không được hỗ trợ: {event_type}")

    @staticmethod
    def mirrored_wheel(
        page: Any,
        point: ViewportPoint,
        delta_x: float,
        delta_y: float,
    ) -> None:
        """Route a mirrored wheel through the same profile-local mouse."""
        page.mouse.move(point.x, point.y)
        page.mouse.wheel(float(delta_x), float(delta_y))

    @staticmethod
    def wheel(cdp: Any, point: ViewportPoint, delta_x: float, delta_y: float) -> None:
        cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": point.x,
                "y": point.y,
                "deltaX": float(delta_x),
                "deltaY": float(delta_y),
                "modifiers": 0,
                "pointerType": "mouse",
            },
        )

    @staticmethod
    def key_event(cdp: Any, params: dict[str, Any]) -> None:
        cdp.send("Input.dispatchKeyEvent", params)

    @staticmethod
    def insert_text(cdp: Any, text: str) -> None:
        cdp.send("Input.insertText", {"text": text})

    @classmethod
    def key_press(
        cls,
        cdp: Any,
        *,
        key: str,
        code: str,
        virtual_key_code: int,
    ) -> None:
        for event_type in ("keyDown", "keyUp"):
            cls.key_event(
                cdp,
                {
                    "type": event_type,
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": virtual_key_code,
                    "nativeVirtualKeyCode": virtual_key_code,
                },
            )
