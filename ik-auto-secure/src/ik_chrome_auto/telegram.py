"""One-way Telegram notifications with local encrypted credential storage."""
from __future__ import annotations

import json
import queue
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from ik_chrome_auto.credential_store import AccountCredential, WindowsCredentialStore


TELEGRAM_CREDENTIAL_ID = "telegram-bot"
_CHAT_ID_MIN_DIGITS = 5
_CHAT_ID_MAX_DIGITS = 20
_BOT_TOKEN_IN_TEXT = re.compile(r"(?<!\d)(\d{5,20}:[A-Za-z0-9_-]{20,})(?![A-Za-z0-9_-])")


class TelegramError(RuntimeError):
    pass


def _describe_http_error(error: urllib.error.HTTPError, action: str) -> TelegramError:
    payload: dict[str, object] | None = None
    try:
        decoded = json.loads(error.read().decode("utf-8"))
        if isinstance(decoded, dict):
            payload = decoded
    except (AttributeError, OSError, ValueError, UnicodeError):
        pass
    is_telegram_response = payload is not None and "error_code" in payload
    if error.code == 404 and is_telegram_response:
        return TelegramError(
            "Telegram không nhận diện Bot Token đang lưu trên máy này. "
            "Hãy dán lại đúng token đang hoạt động ở máy kia hoặc tạo token mới từ @BotFather."
        )
    if error.code == 404:
        return TelegramError(
            "Máy nhận HTTP 404 không phải phản hồi chuẩn của Telegram. "
            "Hãy kiểm tra proxy, VPN, firewall hoặc mạng đang chặn api.telegram.org."
        )
    description = str(payload.get("description", "")) if payload else ""
    return TelegramError(
        f"Telegram từ chối {action}: {description or f'HTTP {error.code}'}"
    )


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: str
    chat_id: str

    def normalized_bot_token(self) -> str:
        token = self.bot_token.strip().strip('"').strip("'")
        embedded = _BOT_TOKEN_IN_TEXT.search(token)
        if embedded:
            return embedded.group(1)
        api_prefix = "https://api.telegram.org/bot"
        if token.lower().startswith(api_prefix):
            token = token[len(api_prefix):].split("/", 1)[0]
        elif token.lower().startswith("bot") and ":" in token[3:]:
            token = token[3:]
        return token.strip()

    def chat_ids(self) -> tuple[str, ...]:
        values = re.split(r"[,;\s]+", self.chat_id.strip())
        return tuple(dict.fromkeys(value for value in values if value))

    def validate(self) -> None:
        token = self.normalized_bot_token()
        # Do not enforce Telegram's token format locally. Telegram may change
        # that format; the Bot API is the only authoritative validator.
        if not token:
            raise ValueError("Vui lòng nhập Bot Token Telegram")
        chat_ids = self.chat_ids()
        if not chat_ids:
            raise ValueError("Vui lòng nhập ít nhất một Chat ID Telegram")
        bot_id = token.split(":", 1)[0] if ":" in token else ""
        for chat_id in chat_ids:
            digits = chat_id[1:] if chat_id.startswith("-") else chat_id
            if (
                not digits.isdigit()
                or not (_CHAT_ID_MIN_DIGITS <= len(digits) <= _CHAT_ID_MAX_DIGITS)
            ):
                raise ValueError(f"Chat ID Telegram không đúng định dạng: {chat_id}")
            if bot_id and chat_id == bot_id:
                raise ValueError(
                    "Chat ID đang là ID của bot. Hãy nhập Chat ID người nhận hoặc group."
                )


def save_telegram_settings(
    settings: TelegramSettings,
    store: WindowsCredentialStore | None = None,
) -> None:
    settings.validate()
    (store or WindowsCredentialStore()).save(
        AccountCredential(
            TELEGRAM_CREDENTIAL_ID,
            ",".join(settings.chat_ids()),
            settings.normalized_bot_token(),
        )
    )


def load_telegram_settings(
    store: WindowsCredentialStore | None = None,
) -> TelegramSettings | None:
    credential = (store or WindowsCredentialStore()).load(TELEGRAM_CREDENTIAL_ID)
    if credential is None:
        return None
    settings = TelegramSettings(credential.password, credential.username)
    settings.validate()
    return settings


def discover_telegram_chat_id(
    bot_token: str,
    *,
    timeout_seconds: float = 8.0,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> str:
    """Return the latest private or group chat that sent a message to this bot."""
    token = TelegramSettings(bot_token, "00000").normalized_bot_token()
    if not token:
        raise ValueError("Vui lòng nhập Bot Token Telegram")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates?limit=50&timeout=0",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _describe_http_error(error, "yêu cầu lấy Chat ID") from error
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as error:
        detail = str(error).replace(token, "***")
        raise TelegramError(f"Không kết nối được Telegram: {detail}") from error
    if not payload.get("ok"):
        raise TelegramError(str(payload.get("description", "Không lấy được Chat ID")))
    for update in reversed(payload.get("result", [])):
        for event_name in ("message", "edited_message", "channel_post"):
            chat = update.get(event_name, {}).get("chat", {})
            chat_id = chat.get("id")
            if chat_id is not None and chat.get("type") in {"private", "group", "supergroup"}:
                return str(chat_id)
    raise TelegramError(
        "Chưa thấy cuộc trò chuyện với bot. Hãy gửi /start trong chat cá nhân hoặc "
        "gửi một tin nhắn trong group rồi thử lại."
    )


def send_telegram_message(
    settings: TelegramSettings,
    text: str,
    *,
    timeout_seconds: float = 8.0,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> None:
    settings.validate()
    errors: list[str] = []
    successful: list[str] = []
    chat_ids = settings.chat_ids()
    for chat_id in chat_ids:
        try:
            _send_telegram_message_to_chat(
                settings,
                chat_id,
                text,
                timeout_seconds=timeout_seconds,
                opener=opener,
            )
            successful.append(chat_id)
        except TelegramError as error:
            errors.append(f"{chat_id}: {error}")
    if errors:
        prefix = (
            f"Đã gửi thành công {len(successful)}/{len(chat_ids)} nơi nhận. "
            if successful
            else ""
        )
        raise TelegramError(prefix + "Không gửi được tới " + "; ".join(errors))


def _send_telegram_message_to_chat(
    settings: TelegramSettings,
    chat_id: str,
    text: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., object],
) -> None:
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.normalized_bot_token()}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _describe_http_error(error, "yêu cầu gửi tin") from error
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as error:
        detail = str(error).replace(settings.normalized_bot_token(), "***")
        raise TelegramError(f"Không kết nối được Telegram: {detail}") from error
    if not result.get("ok"):
        description = str(result.get("description", "Telegram từ chối yêu cầu"))
        raise TelegramError(description)


class TelegramNotifier:
    """Send Telegram messages on one background worker, never on the Qt thread."""

    def __init__(
        self,
        settings: TelegramSettings,
        on_result: Callable[[bool, str], None] | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.on_result = on_result or (lambda _ok, _message: None)
        self._messages: queue.Queue[str | None] = queue.Queue(maxsize=100)
        self._thread = threading.Thread(target=self._run, name="telegram-notifier", daemon=True)
        self._thread.start()

    def notify(self, text: str) -> bool:
        try:
            self._messages.put_nowait(text)
            return True
        except queue.Full:
            self.on_result(False, "Hàng đợi Telegram đã đầy")
            return False

    def close(self) -> None:
        try:
            self._messages.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            message = self._messages.get()
            if message is None:
                return
            try:
                send_telegram_message(self.settings, message)
                self.on_result(True, "Đã gửi thông báo Telegram")
            except Exception as error:
                self.on_result(False, str(error))
