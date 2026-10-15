"""Safe on-exit binary swap must preserve live Chrome data and rollback."""

from __future__ import annotations

import json
from pathlib import Path

from ik_chrome_auto.staged_update import (
    _hash_file, _hash_internal, _internal_fingerprints, _valid_stage, activate_if_idle,
)


def _layout(root: Path) -> tuple[Path, Path]:
    release = root / "release"
    target = release / "IK Auto"
    target.mkdir(parents=True)
    (target / "IK Auto.exe").write_bytes(b"old-binary")
    (target / "_internal").mkdir()
    (target / "_internal" / "old.dll").write_bytes(b"old-lib")
    (target / "config.json").write_text("private config unchanged")
    (target / "build-info.json").write_text(json.dumps({
        "built_at": "previous", "diagnostic_screenshots_dir": "safe-path",
    }))
    (target / "data" / "profiles").mkdir(parents=True)
    (target / "data" / "profiles" / "Cookie Data").write_bytes(b"private")
    stage = release / ".ik-auto-app-next"
    stage.mkdir()
    (stage / "IK Auto.exe").write_bytes(b"new-binary")
    (stage / "_internal").mkdir()
    (stage / "_internal" / "new.dll").write_bytes(b"new-lib")
    (stage / "update.json").write_text(json.dumps({
        "schema": 1, "exe_sha256": _hash_file(stage / "IK Auto.exe"),
        "internal_tree_sha256": _hash_internal(stage / "_internal"),
    }))
    return target, stage


def test_pending_update_does_not_touch_running_app_or_profile(tmp_path: Path) -> None:
    target, stage = _layout(tmp_path)
    assert activate_if_idle(tmp_path, running=lambda root: True) == "application_running"
    assert (target / "IK Auto.exe").read_bytes() == b"old-binary"
    assert (stage / "IK Auto.exe").read_bytes() == b"new-binary"
    assert (target / "data" / "profiles" / "Cookie Data").read_bytes() == b"private"


def test_activates_only_app_binaries_after_exit(tmp_path: Path) -> None:
    target, stage = _layout(tmp_path)
    assert activate_if_idle(tmp_path, running=lambda root: False) == "activated"
    assert (target / "IK Auto.exe").read_bytes() == b"new-binary"
    assert (target / "_internal" / "new.dll").read_bytes() == b"new-lib"
    assert (tmp_path / "release" / ".ik-auto-app-previous" / "IK Auto.exe").read_bytes() == b"old-binary"
    assert (target / "config.json").read_text() == "private config unchanged"
    assert (target / "data" / "profiles" / "Cookie Data").read_bytes() == b"private"
    info = json.loads((target / "build-info.json").read_text())
    assert info["built_at"] != "previous"
    assert info["diagnostic_screenshots_dir"] == "safe-path"
    assert not stage.exists()


def test_activation_preserves_experiment_logs_left_in_staging(tmp_path: Path) -> None:
    target, stage = _layout(tmp_path)
    (stage / "data" / "logs").mkdir(parents=True)
    (stage / "data" / "logs" / "experiment.jsonl").write_text("sample\n")

    assert activate_if_idle(tmp_path, running=lambda root: False) == "activated"
    assert (target / "IK Auto.exe").read_bytes() == b"new-binary"
    assert (target / "config.json").read_text() == "private config unchanged"
    assert (target / "data" / "profiles" / "Cookie Data").read_bytes() == b"private"
    assert not stage.exists()
    archived = list((tmp_path / "release").glob(".ik-auto-stage-preserved-*"))
    assert len(archived) == 1
    assert (archived[0] / "data" / "logs" / "experiment.jsonl").read_text() == "sample\n"


def test_bad_checksum_never_moves_any_binaries(tmp_path: Path) -> None:
    target, stage = _layout(tmp_path)
    (stage / "IK Auto.exe").write_bytes(b"modified")
    assert activate_if_idle(tmp_path, running=lambda root: False) == "stage_integrity_failed"
    assert (target / "IK Auto.exe").read_bytes() == b"old-binary"
    assert (target / "data" / "profiles" / "Cookie Data").read_bytes() == b"private"


def test_failure_during_activation_restores_old_version(tmp_path: Path, monkeypatch) -> None:
    target, stage = _layout(tmp_path)
    old_replace = Path.replace

    def fail_new_exe(path: Path, target_path: Path) -> Path:
        if path == stage / "IK Auto.exe":
            raise PermissionError("simulated file lock")
        return old_replace(path, target_path)

    monkeypatch.setattr(Path, "replace", fail_new_exe)
    outcome = activate_if_idle(tmp_path, running=lambda root: False)
    assert outcome == "activation_failed_rolled_back"
    assert (target / "IK Auto.exe").read_bytes() == b"old-binary"
    assert (target / "_internal" / "old.dll").read_bytes() == b"old-lib"
    assert (target / "data" / "profiles" / "Cookie Data").read_bytes() == b"private"

def test_schema2_file_hashes_verify_independent_of_sort_order(tmp_path: Path) -> None:
    _, stage = _layout(tmp_path)
    source = json.loads((stage / "update.json").read_text())
    entries = _internal_fingerprints(stage / "_internal")
    source["schema"] = 2
    source["internal_files"] = list(reversed(entries))
    (stage / "update.json").write_text(json.dumps(source))
    assert _valid_stage(stage) is True
    (stage / "_internal" / "new.dll").write_bytes(b"tampered")
    assert _valid_stage(stage) is False