from __future__ import annotations

from pathlib import Path

import ik_chrome_auto.update_check as update_check


def test_check_for_git_updates_fetches_and_compares_current_branch(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_git(_root: Path, *args: str) -> str:
        commands.append(args)
        responses = {
            ("branch", "--show-current"): "main",
            ("rev-parse", "--abbrev-ref", "@{upstream}"): "origin/main",
            ("rev-parse", "HEAD"): "local",
            ("rev-parse", "origin/main"): "remote",
            ("rev-list", "--count", "HEAD..origin/main"): "3",
        }
        return responses.get(args, "")

    monkeypatch.setattr(update_check, "_git", fake_git)
    monkeypatch.setattr(update_check, "find_git_workspace", lambda root: root)

    status = update_check.check_for_git_updates(Path("workspace"))

    assert status.update_available is True
    assert status.workspace == Path("workspace")
    assert status.commits_behind == 3
    assert commands[0] == ("fetch", "--quiet", "origin")
