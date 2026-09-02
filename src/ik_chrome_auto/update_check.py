"""Small, explicit Git update check used by the desktop dashboard."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitUpdateStatus:
    workspace: Path
    branch: str
    current_commit: str
    remote_commit: str
    commits_behind: int

    @property
    def update_available(self) -> bool:
        return self.commits_behind > 0


class GitUpdateCheckError(RuntimeError):
    """The update check could not safely establish a Git comparison."""


def find_git_workspace(root: Path) -> Path:
    """Find the repository above a portable release directory, if present."""
    resolved = root.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    raise GitUpdateCheckError("Bản tool này không nằm trong Git workspace nên không thể kiểm tra cập nhật.")


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitUpdateCheckError(f"Không thể chạy Git: {error}") from error
    if result.returncode:
        message = (result.stderr or result.stdout).strip() or "Git trả về lỗi không xác định."
        raise GitUpdateCheckError(message)
    return result.stdout.strip()


def check_for_git_updates(root: Path) -> GitUpdateStatus:
    """Fetch ``origin`` and compare the current branch with its upstream."""
    workspace = find_git_workspace(root)

    _git(workspace, "fetch", "--quiet", "origin")
    branch = _git(workspace, "branch", "--show-current")
    if not branch:
        raise GitUpdateCheckError("Đang ở trạng thái Git không có nhánh hiện tại.")
    try:
        remote = _git(workspace, "rev-parse", "--abbrev-ref", "@{upstream}")
    except GitUpdateCheckError:
        remote = f"origin/{branch}"
    current = _git(workspace, "rev-parse", "HEAD")
    remote_commit = _git(workspace, "rev-parse", remote)
    commits_behind = int(_git(workspace, "rev-list", "--count", f"HEAD..{remote}"))
    return GitUpdateStatus(workspace, branch, current, remote_commit, commits_behind)
