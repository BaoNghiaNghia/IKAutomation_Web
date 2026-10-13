"""Run regression tests with a bounded durable log on busy production hosts.

Does not connect to Chrome. The log is strictly test stdout/stderr and stays
in ignored .build-cache; it must never include account config or passwords.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    report = root / ".build-cache" / "farm-tests-latest.log"
    report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with report.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short"],
                cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                timeout=180, check=False,
            )
        status = result.returncode
    except subprocess.TimeoutExpired:
        status = 124
    text = report.read_text(encoding="utf-8", errors="replace")
    print("exit_code=", status, "test_log=", report, flush=True)
    print(text[-4000:], flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())