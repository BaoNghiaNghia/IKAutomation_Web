"""Release-only build metadata shown by the desktop dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def release_build_label(
    application_directory: Path | None = None,
    *,
    frozen: bool | None = None,
) -> str | None:
    """Return the human-readable build time for a packaged app, if present."""
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not is_frozen:
        return None

    app_dir = application_directory or Path(sys.executable).resolve().parent
    try:
        payload = json.loads((app_dir / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None

    built_at = payload.get("built_at") if isinstance(payload, dict) else None
    return f"Build: {built_at}" if isinstance(built_at, str) and built_at.strip() else None
