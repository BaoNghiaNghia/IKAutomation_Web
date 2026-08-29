import json

from ik_chrome_auto.build_info import (
    release_build_label,
    release_diagnostic_screenshot_directory,
)


def test_development_run_never_exposes_a_build_timestamp(tmp_path) -> None:
    (tmp_path / "build-info.json").write_text(
        json.dumps({"built_at": "29/08/2026 14:30"}), encoding="utf-8"
    )

    assert release_build_label(tmp_path, frozen=False) is None


def test_packaged_run_reads_the_build_timestamp(tmp_path) -> None:
    (tmp_path / "build-info.json").write_text(
        json.dumps({"built_at": "29/08/2026 14:30"}), encoding="utf-8"
    )

    assert release_build_label(tmp_path, frozen=True) == "Build: 29/08/2026 14:30"


def test_packaged_run_hides_the_label_when_metadata_is_missing(tmp_path) -> None:
    assert release_build_label(tmp_path, frozen=True) is None


def test_packaged_run_reads_the_optional_diagnostic_screenshot_directory(tmp_path) -> None:
    expected = tmp_path / "source" / "data" / "screenshots"
    (tmp_path / "build-info.json").write_text(
        json.dumps({"diagnostic_screenshots_dir": str(expected)}), encoding="utf-8"
    )

    assert release_diagnostic_screenshot_directory(tmp_path, frozen=True) == expected


def test_development_run_does_not_mirror_diagnostic_screenshots(tmp_path) -> None:
    (tmp_path / "build-info.json").write_text(
        json.dumps({"diagnostic_screenshots_dir": str(tmp_path / "screenshots")}), encoding="utf-8"
    )

    assert release_diagnostic_screenshot_directory(tmp_path, frozen=False) is None
