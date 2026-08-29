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
    payload = _read_release_build_info(app_dir)

    built_at = payload.get("built_at") if isinstance(payload, dict) else None
    return f"Build: {built_at}" if isinstance(built_at, str) and built_at.strip() else None


def release_diagnostic_screenshot_directory(
    application_directory: Path | None = None,
    *,
    frozen: bool | None = None,
) -> Path | None:
    """Return the optional source diagnostics directory embedded at build time.

    A packaged app always retains its own screenshots inside its ``data``
    directory.  This additional location makes it possible to inspect errors
    from a release build directly in the development workspace as well.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not is_frozen:
        return None

    app_dir = application_directory or Path(sys.executable).resolve().parent
    payload = _read_release_build_info(app_dir)
    configured_directory = (
        payload.get("diagnostic_screenshots_dir") if isinstance(payload, dict) else None
    )
    if not isinstance(configured_directory, str) or not configured_directory.strip():
        return None
    return Path(configured_directory)


def _read_release_build_info(application_directory: Path) -> dict[str, object] | None:
    try:
        payload = json.loads((application_directory / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None
