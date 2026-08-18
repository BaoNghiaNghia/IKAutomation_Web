from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ik_chrome_auto.event_log import JsonLineLog
from ik_chrome_auto.config import is_allowed_url
from ik_chrome_auto.models import CaptureSettings
from ik_chrome_auto.storage import write_retained_json

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Frame, Page, Response, WebSocket


SECRET_KEY_PARTS = (
    "access_token",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "signature",
    "token",
)
REDACTED = "<redacted>"


def is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SECRET_KEY_PARTS)


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_secret_key(str(key)) else redact(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = [
            (key, REDACTED if is_secret_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except ValueError:
        return redact_text(url)


def redact_text(text: str) -> str:
    result = text
    for key in SECRET_KEY_PARTS:
        result = re.sub(
            rf"(?i)({re.escape(key)}[\s\"']*[:=][\s\"']*)[^&\s\"',}}]+",
            rf"\1{REDACTED}",
            result,
        )
    return result


MESSAGE_PROBE = r"""
(() => {
  if (window.__IK_AUTO_PROBE_INSTALLED) return;
  window.__IK_AUTO_PROBE_INSTALLED = true;
  window.__IK_AUTO_MESSAGES = [];
  const secretParts = [
    'access_token', 'authorization', 'cookie', 'password', 'passwd',
    'secret', 'signature', 'token'
  ];
  const isSecret = (key) => {
    const normalized = String(key).toLowerCase().replaceAll('-', '_');
    return secretParts.some((part) => normalized.includes(part));
  };
  const clean = (value, depth = 0, seen = new WeakSet()) => {
    if (depth > 10) return '<max-depth>';
    if (value === null || ['boolean', 'number', 'string'].includes(typeof value)) return value;
    if (typeof value !== 'object') return String(value);
    if (seen.has(value)) return '<circular>';
    seen.add(value);
    if (Array.isArray(value)) return value.slice(0, 200).map((item) => clean(item, depth + 1, seen));
    const output = {};
    for (const [key, item] of Object.entries(value).slice(0, 200)) {
      output[key] = isSecret(key) ? '<redacted>' : clean(item, depth + 1, seen);
    }
    return output;
  };
  window.addEventListener('message', (event) => {
    try {
      window.__IK_AUTO_MESSAGES.push({
        capturedAt: new Date().toISOString(),
        origin: event.origin,
        data: clean(event.data)
      });
      if (window.__IK_AUTO_MESSAGES.length > 250) window.__IK_AUTO_MESSAGES.shift();
    } catch (_) {}
  }, false);
})();
"""


class GameDataReader:
    def __init__(
        self,
        profile_id: str,
        data_dir: Path,
        settings: CaptureSettings,
    ) -> None:
        self.profile_id = profile_id
        self.data_dir = data_dir
        self.settings = settings
        logs_dir = data_dir / "logs"
        self.network_log = JsonLineLog(logs_dir / f"network-{profile_id}.jsonl")
        self.message_log = JsonLineLog(logs_dir / f"messages-{profile_id}.jsonl")
        self.websocket_log = JsonLineLog(logs_dir / f"websocket-{profile_id}.jsonl")
        self._attached_pages: set[int] = set()
        self._seen_messages: set[str] = set()

    def attach(self, context: BrowserContext) -> None:
        if not self.settings.network_capture_enabled:
            return
        context.add_init_script(MESSAGE_PROBE)
        context.on("page", self._attach_page)
        for page in context.pages:
            self._attach_page(page)

    def _attach_page(self, page: Page) -> None:
        identity = id(page)
        if identity in self._attached_pages:
            return
        self._attached_pages.add(identity)
        page.on("response", self._on_response)
        page.on("websocket", self._on_websocket)
        for frame in page.frames:
            try:
                frame.evaluate(MESSAGE_PROBE)
            except Exception:
                pass

    def _matches(self, url: str) -> bool:
        return is_allowed_url(url, self.settings.allowed_hosts)

    def _on_response(self, response: Response) -> None:
        if not self._matches(response.url):
            return
        headers: dict[str, Any]
        try:
            headers = dict(response.headers)
        except Exception:
            headers = {}
        row: dict[str, Any] = {
            "profile_id": self.profile_id,
            "url": redact_url(response.url),
            "status": response.status,
            "method": response.request.method,
            "resource_type": response.request.resource_type,
            "content_type": headers.get("content-type", ""),
        }
        content_type = str(headers.get("content-type", "")).lower()
        should_read = self.settings.capture_response_bodies and (
            "json" in content_type or "text" in content_type or "javascript" in content_type
        )
        if should_read:
            try:
                body = response.body()
                if len(body) <= self.settings.max_body_bytes:
                    text = body.decode("utf-8", errors="replace")
                    try:
                        row["body"] = redact(json.loads(text))
                    except json.JSONDecodeError:
                        row["body"] = redact_text(text[: self.settings.max_text_chars])
                else:
                    row["body_size"] = len(body)
                    row["body_omitted"] = True
            except Exception as error:
                row["body_error"] = type(error).__name__
        self.network_log.write("response", row)

    def _on_websocket(self, socket: WebSocket) -> None:
        safe_url = redact_url(socket.url)

        def received(payload: str | bytes) -> None:
            self._write_websocket("received", safe_url, payload)

        def sent(payload: str | bytes) -> None:
            self._write_websocket("sent", safe_url, payload)

        socket.on("framereceived", received)
        socket.on("framesent", sent)

    def _write_websocket(self, direction: str, url: str, payload: str | bytes) -> None:
        row: dict[str, Any] = {
            "profile_id": self.profile_id,
            "direction": direction,
            "url": url,
        }
        if isinstance(payload, bytes):
            row.update({"kind": "binary", "size": len(payload)})
        else:
            row.update(
                {
                    "kind": "text",
                    "size": len(payload),
                    "data": redact_text(payload[: self.settings.max_text_chars]),
                }
            )
        self.websocket_log.write("websocket_frame", row)

    def snapshot(self, page: Page) -> tuple[dict[str, Any], Path]:
        now = datetime.now(UTC)
        frames = [self._read_frame(frame) for frame in page.frames]
        portal = self._read_portal(page)
        messages = self._collect_messages(page)
        snapshot = {
            "captured_at": now.isoformat(),
            "profile_id": self.profile_id,
            "page": {
                "url": redact_url(page.url),
                "title": self._safe_title(page),
            },
            "portal": portal,
            "frames": frames,
            "new_messages": messages,
        }
        destination = self.data_dir / "snapshots" / self.profile_id
        destination.mkdir(parents=True, exist_ok=True)
        filename = now.strftime("%Y%m%d-%H%M%S-%f") + ".json"
        path = destination / filename
        write_retained_json(path, redact(snapshot), keep=self.settings.snapshot_retention)
        return snapshot, path

    def _safe_title(self, page: Page) -> str:
        try:
            return page.title()
        except Exception:
            return ""

    def _read_portal(self, page: Page) -> dict[str, Any]:
        try:
            raw = page.evaluate(
                """
                () => ({
                  sdkLoaded: Boolean(window.H5SDK),
                  sdkUser: window.H5SDK && window.H5SDK.user ? window.H5SDK.user : null,
                  websiteId: window.websiteId || null,
                  gameId: window.gameId || null,
                  characterLabel: document.querySelector('.txt-name')?.textContent?.trim() || null,
                  serverLabel: document.querySelector('.txt-svr')?.textContent?.trim() || null,
                  iframeSrc: document.querySelector('iframe.iframe')?.src || null
                })
                """
            )
            if raw.get("iframeSrc"):
                raw["iframeSrc"] = redact_url(str(raw["iframeSrc"]))
            return redact(raw)
        except Exception as error:
            return {"error": type(error).__name__}

    def _read_frame(self, frame: Frame) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": frame.name,
            "url": redact_url(frame.url),
        }
        try:
            details = frame.evaluate(
                f"""
                () => {{
                  const canvases = [...document.querySelectorAll('canvas')].map((canvas, index) => {{
                    const box = canvas.getBoundingClientRect();
                    return {{
                      index,
                      width: canvas.width,
                      height: canvas.height,
                      clientWidth: Math.round(box.width),
                      clientHeight: Math.round(box.height),
                      visible: box.width > 0 && box.height > 0
                    }};
                  }});
                  return {{
                    title: document.title || '',
                    readyState: document.readyState,
                    canvasCount: canvases.length,
                    canvases,
                    elementCount: document.querySelectorAll('*').length,
                    visibleText: (document.body?.innerText || '').slice(0, {self.settings.max_text_chars})
                  }};
                }}
                """
            )
            row.update(redact(details))
        except Exception as error:
            row["read_error"] = type(error).__name__
        return row

    def _collect_messages(self, page: Page) -> list[dict[str, Any]]:
        new_messages: list[dict[str, Any]] = []
        for frame in page.frames:
            try:
                messages = frame.evaluate("() => window.__IK_AUTO_MESSAGES || []")
            except Exception:
                continue
            for message in messages:
                safe = redact(message)
                fingerprint = json.dumps(safe, sort_keys=True, ensure_ascii=False, default=str)
                if fingerprint in self._seen_messages:
                    continue
                self._seen_messages.add(fingerprint)
                safe_row = {
                    "profile_id": self.profile_id,
                    "frame_url": redact_url(frame.url),
                    "message": safe,
                }
                self.message_log.write("post_message", safe_row)
                new_messages.append(safe_row)
        if len(self._seen_messages) > 5_000:
            self._seen_messages.clear()
        return new_messages
