from __future__ import annotations

import os
from pathlib import Path

from ik_chrome_auto.storage import write_retained_json, write_retained_png


def test_profile_screenshot_folder_keeps_only_two_newest_images(tmp_path: Path) -> None:
    folder = tmp_path / "screenshots" / "farm-1"
    written: list[Path] = []
    for index in range(4):
        path = folder / f"debug-{index}.png"
        write_retained_png(path, f"png-{index}".encode(), keep=2)
        timestamp = 1_700_000_000_000_000_000 + index
        os.utime(path, ns=(timestamp, timestamp))
        written.append(path)

    # Trigger pruning once more after the deterministic timestamps above.
    newest = folder / "debug-4.png"
    write_retained_png(newest, b"png-4", keep=2)

    remaining = {path.name for path in folder.glob("*.png")}
    assert remaining == {"debug-3.png", "debug-4.png"}
    assert all(not path.exists() for path in written[:3])


def test_snapshot_folder_keeps_only_configured_number_of_json_files(tmp_path: Path) -> None:
    folder = tmp_path / "snapshots" / "farm-1"
    for index in range(4):
        path = folder / f"snapshot-{index}.json"
        write_retained_json(path, {"index": index}, keep=2)
        timestamp = 1_700_000_000_000_000_000 + index
        os.utime(path, ns=(timestamp, timestamp))

    write_retained_json(folder / "snapshot-4.json", {"index": 4}, keep=2)

    assert {path.name for path in folder.glob("*.json")} == {
        "snapshot-3.json",
        "snapshot-4.json",
    }
