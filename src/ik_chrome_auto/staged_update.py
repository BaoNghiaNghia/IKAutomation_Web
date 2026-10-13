"""Safe, offline installation of a prebuilt IK Auto application package.

NEVER closes Chrome, never loads cookies, and never changes config.json or
data/profiles. Running desktop UI always wins: activation waits for app exit.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import subprocess
from datetime import datetime
from collections.abc import Callable
from pathlib import Path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _internal_fingerprints(internal: Path) -> list[str]:
    fingerprints: list[str] = []
    files = sorted(internal.rglob("*"), key=lambda p: str(p).casefold())
    for file in files:
        if file.is_symlink():
            raise ValueError("Staged symlink is not allowed")
        if file.is_file():
            relative = str(file.relative_to(internal)).replace("/", "\\")
            fingerprints.append(f"{relative}|{_hash_file(file)}")
    if not fingerprints:
        raise ValueError("Empty _internal package")
    return fingerprints


def _hash_internal(internal: Path) -> str:
    return hashlib.sha256(
        "\n".join(_internal_fingerprints(internal)).encode("utf-8")
    ).hexdigest()


def _valid_stage(stage: Path) -> bool:
    exe, internal, manifest = (
        stage / "IK Auto.exe", stage / "_internal", stage / "update.json"
    )
    if not exe.is_file() or not internal.is_dir() or not manifest.is_file():
        return False
    try:
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if document.get("exe_sha256") != _hash_file(exe):
            return False
        if document.get("schema") == 2:
            expected = document.get("internal_files")
            if not isinstance(expected, list) or not all(
                isinstance(entry, str) for entry in expected
            ):
                return False
            actual = _internal_fingerprints(internal)
            # Compare a complete file set, independent of WinPS/Python sort
            # collation, and reject duplicate/missing manifest entries.
            return (
                len(expected) == len(actual)
                and len(set(expected)) == len(expected)
                and set(expected) == set(actual)
            )
        return (
            document.get("schema") == 1
            and document.get("internal_tree_sha256") == _hash_internal(internal)
        )
    except (OSError, ValueError, UnicodeDecodeError, TypeError):
        return False


def packaged_app_running(root: Path) -> bool:
    """Fail closed if IK Auto exists or the Windows process list fails.

    Never inspect command-line arguments or other apps' environment. A
    matching process name on another installation is treated conservatively
    as active; this may delay an update but cannot close or corrupt Chrome.
    """
    del root
    try:
        proc = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"], check=True,
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return True
    return any(
        row and row[0].strip().casefold() == "ik auto.exe"
        for row in csv.reader(io.StringIO(proc.stdout))
    )

def activate_if_idle(
    project_root: Path, *, running: Callable[[Path], bool] = packaged_app_running
) -> str:
    """Activate only two known binary paths; use old binaries for rollback."""
    release = project_root / "release"
    stage = release / ".ik-auto-app-next"
    manifest = stage / "update.json"
    if not manifest.is_file():
        return "nothing_staged"
    if running(project_root):
        return "application_running"
    if not _valid_stage(stage):
        return "stage_integrity_failed"
    target = release / "IK Auto"
    backup = release / ".ik-auto-app-previous"
    rollback = release / ".ik-auto-app-rollback-pending"
    exe, internal = target / "IK Auto.exe", target / "_internal"
    if not exe.is_file() or not internal.is_dir() or rollback.exists():
        return "manual_recovery_required"
    # Read the process table again immediately before moving either binary.
    if running(project_root):
        return "application_running"
    rollback.mkdir()
    old_exe_moved = old_internal_moved = False
    new_exe_moved = new_internal_moved = False
    try:
        exe.replace(rollback / "IK Auto.exe")
        old_exe_moved = True
        internal.replace(rollback / "_internal")
        old_internal_moved = True
        (stage / "_internal").replace(internal)
        new_internal_moved = True
        (stage / "IK Auto.exe").replace(exe)
        new_exe_moved = True
    except OSError:
        try:
            if new_exe_moved and exe.exists():
                exe.unlink()
            if new_internal_moved and internal.exists():
                shutil.rmtree(internal)
            if old_internal_moved:
                (rollback / "_internal").replace(internal)
            if old_exe_moved:
                (rollback / "IK Auto.exe").replace(exe)
        except OSError:
            return "rollback_failed_manual_recovery_required"
        if rollback.exists():
            rollback.rmdir()
        return "activation_failed_rolled_back"
    # New binary has been moved into place. Keep its last-known-good
    # predecessor for rollback; remove ONLY the old binary backup.
    if backup.exists():
        shutil.rmtree(backup)
    rollback.replace(backup)
    # Release UI reads build-info.json next to the EXE. It must reflect the
    # staged build rather than the previously installed application's date.
    # Preserve the pre-existing diagnostic directory metadata.
    try:
        info_path = target / "build-info.json"
        info = (
            json.loads(info_path.read_text(encoding="utf-8-sig"))
            if info_path.is_file() else {}
        )
        if not isinstance(info, dict):
            info = {}
        stage_info = json.loads(manifest.read_text(encoding="utf-8-sig"))
        info["built_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        signature = stage_info.get("package_signature")
        if isinstance(signature, str):
            info["source_signature"] = signature[:12]
        temp_info = target / ".build-info-stage.tmp"
        temp_info.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        temp_info.replace(info_path)
    except (OSError, ValueError, TypeError):
        # Metadata is advisory; successful binary activation remains valid.
        pass
    manifest.unlink()
    stage.rmdir()
    return "activated"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.verify_only:
            stage = root / "release" / ".ik-auto-app-next"
            outcome = "stage_verified" if _valid_stage(stage) else "stage_integrity_failed"
        else:
            outcome = activate_if_idle(root)
    except (OSError, ValueError) as error:
        outcome = "update_error_" + type(error).__name__
    print(json.dumps({"result": outcome}))
    return 0 if outcome in {
        "nothing_staged", "application_running", "activated", "stage_verified",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())