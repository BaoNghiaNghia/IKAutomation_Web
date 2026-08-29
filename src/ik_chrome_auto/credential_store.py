"""Windows Credential Manager integration for local game-account credentials."""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
TARGET_PREFIX = "IKAutoSecure/account/"
_ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AccountCredential:
    account_id: str
    username: str
    password: str


def escape_account_export_field(value: str) -> str:
    """Escape one field for the portable account-export text format."""
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace("|", "\\|")
    )


def parse_account_export_line(line: str) -> tuple[str, str, str] | None:
    """Read one ``profile|username|password`` line produced by the exporter.

    The profile label is retained for format validation. New profiles receive
    their own local IDs when the account dialog is saved, so an imported file
    can safely be used on another computer.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            if character == "n":
                current.append("\n")
            elif character in {"\\", "|"}:
                current.append(character)
            else:
                # Keep a literal backslash when the source was not generated
                # by this exporter, instead of silently changing a password.
                current.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    fields.append("".join(current))

    if len(fields) != 3:
        return None
    profile_name, username, password = fields
    profile_name = profile_name.strip()
    username = username.strip()
    if (
        not profile_name
        or not username
        or not password
        or any(character.isspace() for character in username)
    ):
        return None
    return profile_name, username, password


def target_for(account_id: str) -> str:
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("account_id chỉ gồm a-z, 0-9, - hoặc _, tối đa 64 ký tự")
    return TARGET_PREFIX + account_id


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Stores one generic credential per account in the current user's Vault."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CredentialError("Windows Credential Manager chỉ hỗ trợ Windows")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]

    def save(self, credential: AccountCredential) -> None:
        target = target_for(credential.account_id)
        if not credential.username or not credential.password:
            raise ValueError("username và password không được để trống")
        encoded = credential.password.encode("utf-16-le")
        if len(encoded) > 2_560:
            raise ValueError("password vượt kích thước Credential Manager cho phép")
        buffer = ctypes.create_string_buffer(encoded)
        try:
            native = _CREDENTIALW()
            native.Type = CRED_TYPE_GENERIC
            native.TargetName = target
            native.CredentialBlobSize = len(encoded)
            native.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            native.Persist = CRED_PERSIST_LOCAL_MACHINE
            native.UserName = credential.username
            if not self._advapi32.CredWriteW(ctypes.byref(native), 0):
                raise CredentialError(f"Không ghi được Windows Vault (WinError {ctypes.get_last_error()})")
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def load(self, account_id: str) -> AccountCredential | None:
        target = target_for(account_id)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"Không đọc được Windows Vault (WinError {error})")
        try:
            native = pointer.contents
            secret = ctypes.string_at(native.CredentialBlob, native.CredentialBlobSize).decode("utf-16-le")
            return AccountCredential(account_id, native.UserName or "", secret)
        finally:
            self._advapi32.CredFree(pointer)

    def exists(self, account_id: str) -> bool:
        credential = self.load(account_id)
        return credential is not None

    def delete(self, account_id: str) -> bool:
        target = target_for(account_id)
        if self._advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return False
        raise CredentialError(f"Không xóa được Windows Vault entry (WinError {error})")
