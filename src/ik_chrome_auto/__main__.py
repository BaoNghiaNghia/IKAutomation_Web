from __future__ import annotations

import argparse
import getpass
import sys
import threading
from pathlib import Path

from ik_chrome_auto.actions import AutomationFunctions
from ik_chrome_auto.browser import ChromeProfileSession
from ik_chrome_auto.config import ensure_data_dirs, load_config
from ik_chrome_auto.doctor import run_doctor


def run_credential_command(action: str, account_id: str, username: str | None = None) -> int:
    from ik_chrome_auto.credential_store import AccountCredential, WindowsCredentialStore

    store = WindowsCredentialStore()
    if action == "set":
        safe_username = username or input("Game username: ").strip()
        password = getpass.getpass("Game password (hidden): ")
        store.save(AccountCredential(account_id, safe_username, password))
        print(f"Đã lưu credential cho account '{account_id}' trong Windows Vault.")
        return 0
    if action == "verify":
        print("Có credential." if store.exists(account_id) else "Không tìm thấy credential.")
        return 0 if store.exists(account_id) else 1
    if action == "delete":
        deleted = store.delete(account_id)
        print("Đã xóa credential." if deleted else "Không tìm thấy credential.")
        return 0
    raise ValueError(f"Credential action không hỗ trợ: {action}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IK Chrome Auto")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("ui")
    subparsers.add_parser("doctor")
    subparsers.add_parser("list")
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("profile")
    screenshot_parser = subparsers.add_parser("screenshot")
    screenshot_parser.add_argument("profile")
    credential_parser = subparsers.add_parser("credential", help="Quản lý Windows Vault credential")
    credential_subparsers = credential_parser.add_subparsers(dest="credential_action", required=True)
    credential_set = credential_subparsers.add_parser("set")
    credential_set.add_argument("account_id")
    credential_set.add_argument("--username")
    credential_verify = credential_subparsers.add_parser("verify")
    credential_verify.add_argument("account_id")
    credential_delete = credential_subparsers.add_parser("delete")
    credential_delete.add_argument("account_id")
    return parser


def run_one(config_path: Path, profile_id: str, operation: str) -> int:
    config = load_config(config_path)
    ensure_data_dirs(config)
    profile = config.profile(profile_id)
    session = ChromeProfileSession(config, profile)
    stop_event = threading.Event()
    try:
        session.start(navigate=True)
        functions = AutomationFunctions(session, stop_event, lambda message: print(message))
        if operation == "inspect":
            print(functions.read_state())
        elif operation == "screenshot":
            print(functions.screenshot("cli"))
        return 0
    finally:
        session.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "ui"
    config_path = args.config.resolve()
    if command == "ui":
        from ik_chrome_auto.dashboard import run_dashboard

        run_dashboard(config_path)
    elif command == "doctor":
        raise SystemExit(run_doctor(config_path))
    elif command == "list":
        config = load_config(config_path)
        for profile in config.profiles:
            print(f"{profile.id}\t{profile.mode.value}\t{profile.name}")
    elif command == "inspect":
        raise SystemExit(run_one(config_path, args.profile, "inspect"))
    elif command == "screenshot":
        raise SystemExit(run_one(config_path, args.profile, "screenshot"))
    elif command == "credential":
        raise SystemExit(run_credential_command(args.credential_action, args.account_id, getattr(args, "username", None)))
    else:
        parser.error(f"Lệnh không hỗ trợ: {command}")


if __name__ == "__main__":
    main()
