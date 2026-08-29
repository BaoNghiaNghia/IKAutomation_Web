"""Runtime font selection for the desktop dashboard."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from tkinter import font as tkfont


_FR_PRIVATE = 0x10
_INTER_FILE = Path(__file__).with_name("assets") / "Inter-Variable.ttf"


def dashboard_font_family(root: object) -> str:
    """Return Inter, loading the bundled Google Fonts file privately if needed."""
    try:
        available = set(tkfont.families(root))
    except Exception:
        available = set()
    if "Inter" in available:
        return "Inter"
    if sys.platform == "win32" and _INTER_FILE.is_file():
        try:
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            gdi32.AddFontResourceExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            gdi32.AddFontResourceExW.restype = ctypes.c_int
            if gdi32.AddFontResourceExW(str(_INTER_FILE), _FR_PRIVATE, None) > 0:
                return "Inter"
        except Exception:
            pass
    return "Segoe UI"
