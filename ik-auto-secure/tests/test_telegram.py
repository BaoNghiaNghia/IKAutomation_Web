from __future__ import annotations

import json
from io import BytesIO
import urllib.error

import pytest

from ik_chrome_auto.credential_store import AccountCredential
from ik_chrome_auto.telegram import (
    TELEGRAM_CREDENTIAL_ID,
    TelegramSettings,
    discover_telegram_chat_id,
    load_telegram_settings,
    save_telegram_settings,
    send_telegram_message,
)


class FakeStore:
    def __init__(self) -> None:
        self.credential: AccountCredential | None = None

    def save(self, credential: AccountCredential) -> None:
        self.credential = credential

    def load(self, _account_id: str) -> AccountCredential | None:
        return self.credential


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return b'{"ok":true}'


def test_telegram_settings_are_stored_as_windows_credential() -> None:
    store = FakeStore()
    settings = TelegramSettings("123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345", "-123456789")

    save_telegram_settings(settings, store)

    assert store.credential == AccountCredential(
        TELEGRAM_CREDENTIAL_ID,
        "-123456789",
        "123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345",
    )
    assert load_telegram_settings(store) == settings


def test_send_telegram_message_uses_post_json() -> None:
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    settings = TelegramSettings("123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345", "987654321")
    send_telegram_message(settings, "Thông báo thử", opener=opener)

    assert captured["url"].endswith("/sendMessage")
    assert captured["payload"] == {
        "chat_id": "987654321",
        "text": "Thông báo thử",
        "disable_web_page_preview": True,
    }
    assert captured["timeout"] == 8.0


def test_telegram_does_not_validate_bot_token_format_locally() -> None:
    TelegramSettings("token-do-telegram-api-xac-minh", "123456789").validate()


def test_telegram_requires_a_non_empty_bot_token() -> None:
    with pytest.raises(ValueError, match="Bot Token"):
        TelegramSettings("   ", "123456789").validate()


def test_telegram_accepts_newer_long_bot_identifiers() -> None:
    TelegramSettings(
        "123456789012345:abcdefghijklmnopqrstuvwxyzABCDE_12345",
        "8912704461",
    ).validate()


@pytest.mark.parametrize(
    ("raw_token", "expected"),
    [
        ("bot123456789:secret-value", "123456789:secret-value"),
        (
            "https://api.telegram.org/bot123456789:secret-value/sendMessage",
            "123456789:secret-value",
        ),
        ('"123456789:secret-value"', "123456789:secret-value"),
        (
            "Use this token to access the HTTP API:"
            "8912704461:AAG2kNVn4LOYabcdefghijklmnopqrstuvwxyz "
            "Keep your token secure.",
            "8912704461:AAG2kNVn4LOYabcdefghijklmnopqrstuvwxyz",
        ),
    ],
)
def test_telegram_normalizes_common_bot_token_inputs(raw_token, expected) -> None:
    assert TelegramSettings(raw_token, "123456789").normalized_bot_token() == expected


def test_telegram_reports_invalid_token_for_http_404() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            {},
            BytesIO(b'{"ok":false,"error_code":404,"description":"Not Found"}'),
        )

    with pytest.raises(Exception, match="không nhận diện Bot Token"):
        send_telegram_message(
            TelegramSettings("123456789:invalid", "987654321"),
            "test",
            opener=opener,
        )


def test_telegram_distinguishes_proxy_404_from_invalid_token() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            {},
            BytesIO(b"<html>proxy not found</html>"),
        )

    with pytest.raises(Exception, match="proxy, VPN, firewall"):
        send_telegram_message(
            TelegramSettings("123456789:invalid", "987654321"),
            "test",
            opener=opener,
        )


def test_telegram_rejects_bot_id_used_as_recipient_chat_id() -> None:
    with pytest.raises(ValueError, match="ID của bot"):
        TelegramSettings("8912704461:secret-value", "8912704461").validate()


def test_discover_telegram_chat_id_uses_latest_private_message() -> None:
    class UpdatesResponse(FakeResponse):
        def read(self) -> bytes:
            return json.dumps(
                {
                    "ok": True,
                    "result": [
                        {"message": {"chat": {"id": 111, "type": "private"}}},
                        {"message": {"chat": {"id": 222, "type": "private"}}},
                    ],
                }
            ).encode("utf-8")

    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return UpdatesResponse()

    chat_id = discover_telegram_chat_id(
        "Use this token: 8912704461:AAG2kNVn4LOYabcdefghijklmnopqrstuvwxyz",
        opener=opener,
    )

    assert chat_id == "222"
    assert "/getUpdates?" in captured["url"]
    assert captured["timeout"] == 8.0


def test_send_telegram_message_sends_to_every_configured_chat() -> None:
    chat_ids = []

    def opener(request, timeout):
        chat_ids.append(json.loads(request.data.decode("utf-8"))["chat_id"])
        return FakeResponse()

    send_telegram_message(
        TelegramSettings(
            "123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345",
            "987654321, -1001234567890; 555666777",
        ),
        "Thông báo nhiều nơi",
        opener=opener,
    )

    assert chat_ids == ["987654321", "-1001234567890", "555666777"]


def test_discover_telegram_chat_id_accepts_supergroup() -> None:
    class GroupResponse(FakeResponse):
        def read(self) -> bytes:
            return json.dumps(
                {
                    "ok": True,
                    "result": [
                        {"message": {"chat": {"id": -1001234567890, "type": "supergroup"}}}
                    ],
                }
            ).encode("utf-8")

    assert discover_telegram_chat_id(
        "123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345",
        opener=lambda request, timeout: GroupResponse(),
    ) == "-1001234567890"


def test_send_reports_partial_success_and_continues_other_chats() -> None:
    attempted = []

    def opener(request, timeout):
        chat_id = json.loads(request.data.decode("utf-8"))["chat_id"]
        attempted.append(chat_id)
        if chat_id == "8651837410":
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                BytesIO(
                    b'{"ok":false,"error_code":400,'
                    b'"description":"Bad Request: chat not found"}'
                ),
            )
        return FakeResponse()

    with pytest.raises(Exception, match="Đã gửi thành công 1/2"):
        send_telegram_message(
            TelegramSettings(
                "123456789:abcdefghijklmnopqrstuvwxyzABCDE_12345",
                "8651837410,-5171665518",
            ),
            "test",
            opener=opener,
        )

    assert attempted == ["8651837410", "-5171665518"]
