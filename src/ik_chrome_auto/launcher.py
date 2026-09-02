"""Entry point used by the packaged Windows desktop application."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


# Keep the handles alive for the life of the process.  Closing an
# ``add_dll_directory`` handle removes that directory from Windows' DLL search
# path again.
_dll_directory_handles: list[object] = []


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


def _configure_packaged_dll_search_paths(resources: Path) -> None:
    """Prioritize the Qt runtime shipped alongside a frozen application.

    ``QtCore.pyd`` depends on ``Qt6Core.dll``.  A machine can also have another
    Qt installation on PATH (for example from a different Python tool), and
    Windows may load that incompatible DLL first.  Registering the bundled
    PySide6 directory before importing the dashboard guarantees the extension
    and its Qt DLL are from the same PySide6 build.
    """
    if not getattr(sys, "frozen", False):
        return

    runtime_dirs = (resources / "PySide6", resources / "shiboken6")
    existing_dirs = [directory for directory in runtime_dirs if directory.is_dir()]
    if not existing_dirs:
        return

    path_prefix = os.pathsep.join(str(directory) for directory in existing_dirs)
    existing_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{path_prefix}{os.pathsep}{existing_path}" if existing_path else path_prefix

    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        _dll_directory_handles.extend(add_dll_directory(str(directory)) for directory in existing_dirs)


def main() -> None:
    app_dir = _application_directory()
    runtime_dir = _runtime_directory(app_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime_dir)
    config = runtime_dir / "config.json"
    resources = Path(getattr(sys, "_MEIPASS", app_dir))
    _configure_packaged_dll_search_paths(resources)
    example = resources / "config.example.json"
    if not config.exists() and example.exists():
        shutil.copyfile(example, config)
    from ik_chrome_auto.__main__ import main as run_application

    run_application()


if __name__ == "__main__":
    main()
