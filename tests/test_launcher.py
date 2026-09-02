from __future__ import annotations

from ik_chrome_auto import launcher


def test_configure_packaged_dll_search_paths_uses_bundled_qt_first(monkeypatch) -> None:
    resources = launcher.Path("bundle")
    pyside_dir = resources / "PySide6"
    shiboken_dir = resources / "shiboken6"
    registered: list[str] = []

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        launcher.Path,
        "is_dir",
        lambda path: path.name in {"PySide6", "shiboken6"},
    )
    def register_dll_directory(path: str) -> str:
        registered.append(path)
        return path

    monkeypatch.setattr(launcher.os, "add_dll_directory", register_dll_directory)
    monkeypatch.setenv("PATH", "C:\\external-qt")
    launcher._dll_directory_handles.clear()

    launcher._configure_packaged_dll_search_paths(resources)

    assert registered == [str(pyside_dir), str(shiboken_dir)]
    assert launcher.os.environ["PATH"].startswith(f"{pyside_dir};{shiboken_dir};")
    assert launcher._dll_directory_handles == [str(pyside_dir), str(shiboken_dir)]
