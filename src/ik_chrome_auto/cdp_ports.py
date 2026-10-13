"""Stable CDP port helpers shared by browser startup and config.

Split out of ``browser.py``/``windows.py`` so port parsing lives in one
place. ``browser.py`` keeps backward-compatible aliases because tests
monkeypatch ``browser._profile_cdp_port`` and
``browser.find_tcp_listener_process``.
"""
from __future__ import annotations

import re


def stable_profile_cdp_port(profile_id: str) -> int:
    """Stable DevTools port without persisting runtime state."""
    from ik_chrome_auto.config import _legacy_cdp_port

    return _legacy_cdp_port(profile_id)


def command_line_user_data_dir(command_line: str) -> str | None:
    """Extract the full path, including spaces, from Windows Chrome argv.

    subprocess.list2cmdline quotes the *whole* argument when the path has
    spaces: "--user-data-dir=D:\\IK Auto\\profiles\\account-3". Older
    parsing looked for quotes after '=' only and truncated this to D:\\IK.
    """
    match = re.search(
        r"""(?:"--user-data-dir=([^"]+)"|'--user-data-dir=([^']+)'|"""
        r"""--user-data-dir(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s"']+)))""",
        command_line,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return next((value for value in match.groups() if value is not None), None)


def command_line_remote_debugging_port(command_line: str) -> int | None:
    match = re.search(
        r"--remote-debugging-port(?:=|\s+)(\d+)",
        command_line,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None
