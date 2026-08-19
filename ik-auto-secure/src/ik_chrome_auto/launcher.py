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


def main() -> None:
    app_dir = _application_directory()
    os.chdir(app_dir)
    config = app_dir / "config.json"
    resources = Path(getattr(sys, "_MEIPASS", app_dir))
    example = resources / "config.example.json"
    if not config.exists() and example.exists():
        shutil.copyfile(example, config)
    from ik_chrome_auto.__main__ import main as run_application

    run_application()


if __name__ == "__main__":
    main()
