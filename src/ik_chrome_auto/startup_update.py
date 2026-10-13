"""Startup-only, fail-open Git updater for source and Windows packaged builds.

Updates are never applied while the dashboard/Chrome workers are running.
A separate process waits for this instance to exit before rebuilding a release.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ik_chrome_auto.diagnostics import diagnostic_log, record
from ik_chrome_auto.update_check import (
    GitUpdateCheckError,
    _git,
    check_for_git_updates,
    find_git_workspace,
)


def maybe_start_update(
    config_path: Path,
    *,
    frozen: bool | None = None,
    executable: Path | None = None,
) -> bool:
    """Return True only if an external updater has been started successfully.

    A network failure, no Git checkout, dirty tree, or a divergent history
    always falls back to launching the existing tool without losing state.
    """
    log = diagnostic_log(config_path)
    if os.environ.get("IK_AUTO_SKIP_STARTUP_UPDATE") == "1":
        record(log, "startup_update_skipped", reason="restart_guard")
        return False
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    exe = executable or Path(sys.executable)
    try:
        root = find_git_workspace(exe.parent if is_frozen else config_path.parent)
        script = root / "scripts" / "update-on-launch.ps1"
        if not script.is_file() or os.name != "nt":
            record(log, "startup_update_skipped", reason="updater_not_available")
            return False
        # Inspect local state before contacting GitHub: a work-in-progress
        # checkout must open immediately, even when the network is offline.
        if _git(root, "status", "--porcelain", "--untracked-files=normal").strip():
            record(log, "startup_update_skipped", reason="working_tree_dirty")
            return False
        if _git(root, "branch", "--show-current") != "main":
            record(log, "startup_update_skipped", reason="non_main_branch")
            return False
        if _git(root, "rev-parse", "--abbrev-ref", "@{upstream}") != "origin/main":
            record(log, "startup_update_skipped", reason="unexpected_upstream")
            return False
        status = check_for_git_updates(root)
        if not status.update_available:
            record(log, "startup_update_current", revision=status.current_commit[:12])
            return False
        _git(root, "merge-base", "--is-ancestor", "HEAD", "origin/main")
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-ProjectRoot", str(root),
            "-ParentPid", str(os.getpid()),
            "-LaunchMode", "release" if is_frozen else "dev",
            "-ExecutablePath", str(exe),
        ]
        subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        record(
            log, "startup_update_scheduled", commits_behind=status.commits_behind,
            revision=status.current_commit[:12], target=status.remote_commit[:12],
            mode="release" if is_frozen else "dev",
        )
        return True
    except (GitUpdateCheckError, OSError, subprocess.SubprocessError, ValueError) as error:
        record(log, "startup_update_failed_open", error_type=type(error).__name__)
        return False