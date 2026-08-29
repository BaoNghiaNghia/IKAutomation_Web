"""Entry point used by the packaged Windows desktop application."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _runtime_directory(app_dir: Path) -> Path:
    """Keep mutable data outside a freshly built release directory.

    Existing portable installs with a config next to the executable remain
    supported, so an update never silently abandons established profiles.
    """
    legacy_config = app_dir / "config.json"
    if not getattr(sys, "frozen", False) or legacy_config.exists():
        return app_dir
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "IK Auto"
    return app_dir


def main() -> None:
    app_dir = _application_directory()
    runtime_dir = _runtime_directory(app_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime_dir)
    config = runtime_dir / "config.json"
    resources = Path(getattr(sys, "_MEIPASS", app_dir))
    example = resources / "config.example.json"
    if not config.exists() and example.exists():
        shutil.copyfile(example, config)
    from ik_chrome_auto.__main__ import main as run_application

    run_application()


if __name__ == "__main__":
    main()
