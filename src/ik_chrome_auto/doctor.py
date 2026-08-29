from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ik_chrome_auto.browser import find_chrome
from ik_chrome_auto.config import ensure_data_dirs, load_config
from ik_chrome_auto.interaction import validate_viewport
from ik_chrome_auto.models import ProfileMode


def run_doctor(config_path: Path) -> int:
    failures = 0

    def report(ok: bool, label: str, detail: str) -> None:
        nonlocal failures
        marker = "OK" if ok else "LOI"
        print(f"[{marker}] {label}: {detail}")
        if not ok:
            failures += 1

    version_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    report(version_ok, "Python", sys.version.split()[0])
    try:
        config = load_config(config_path)
        ensure_data_dirs(config)
        report(True, "Config", str(config.source))
    except Exception as error:
        report(False, "Config", str(error))
        return 1

    chrome = find_chrome(config.browser.chrome_executable)
    report(chrome is not None, "Google Chrome", str(chrome or "không tìm thấy"))
    try:
        width, height = validate_viewport(
            config.browser.viewport_width,
            config.browser.viewport_height,
        )
        report(
            True,
            "Viewport",
            f"{width}x{height} px; auto_resize={config.browser.auto_resize}",
        )
    except ValueError as error:
        report(False, "Viewport", str(error))
    report(
        True,
        "Chrome window",
        (
            f"app_mode={config.browser.app_mode}; "
            f"profile_title={config.browser.profile_title}; "
            "automation_banner=visible"
        ),
    )
    try:
        import playwright  # noqa: F401

        report(True, "Playwright", "đã cài")
    except ImportError:
        report(False, "Playwright", "chạy setup.cmd")

    report(bool(config.profiles), "Profiles", f"{len(config.profiles)} profile")
    for profile in config.profiles:
        if profile.mode == ProfileMode.MANAGED:
            ok = profile.user_data_dir is not None
            detail = str(profile.user_data_dir or "thiếu user_data_dir")
        else:
            ok = bool(profile.cdp_url)
            detail = str(profile.cdp_url or "thiếu cdp_url")
        report(ok, f"Profile {profile.id}", detail)

    if failures:
        print(f"\nCó {failures} lỗi cần xử lý.")
        return 1
    print("\nMôi trường sẵn sàng.")
    return 0


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Kiểm tra IK Chrome Auto")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()
    raise SystemExit(run_doctor(args.config))


if __name__ == "__main__":
    main()
