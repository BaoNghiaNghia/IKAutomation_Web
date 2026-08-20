from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ik_chrome_auto.models import (
    AppConfig,
    BrowserSettings,
    CaptureSettings,
    ProfileConfig,
    ProfileMode,
)


def _resolve(root: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _relative_or_absolute(root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-_")
    return value or "profile"


def unique_profile_id(name: str, existing: set[str]) -> str:
    base = slugify(name)
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


DEFAULT_ALLOWED_HOSTS = ("ik.playfun.vn", "gtarcade.com", "smobgame.com")


def is_allowed_host(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Match an exact host or a real subdomain, never a URL substring."""
    normalized = host.rstrip(".").lower()
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in allowed_hosts
    )


def is_allowed_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname) and is_allowed_host(
        parts.hostname, allowed_hosts
    )


def load_config(path: Path) -> AppConfig:
    source = path.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Không tìm thấy config: {source}")
    raw: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    root = source.parent
    browser_raw = raw.get("browser", {})
    viewport = browser_raw.get("viewport", {})
    capture_raw = raw.get("capture", {})

    browser = BrowserSettings(
        chrome_executable=str(browser_raw.get("chrome_executable", "auto")),
        headless=bool(browser_raw.get("headless", False)),
        app_mode=bool(browser_raw.get("app_mode", True)),
        profile_title=bool(browser_raw.get("profile_title", True)),
        low_memory_mode=bool(browser_raw.get("low_memory_mode", True)),
        auto_resize=bool(browser_raw.get("auto_resize", True)),
        viewport_width=int(viewport.get("width", 500)),
        viewport_height=int(viewport.get("height", 281)),
        windows_per_row=min(6, max(2, int(browser_raw.get("windows_per_row", 6)))),
        slow_mo_ms=int(browser_raw.get("slow_mo_ms", 0)),
        startup_timeout_ms=int(browser_raw.get("startup_timeout_ms", 90_000)),
    )
    allowed_hosts = tuple(
        str(item).strip().lower().lstrip(".")
        for item in capture_raw.get("allowed_hosts", DEFAULT_ALLOWED_HOSTS)
        if str(item).strip()
    )
    if not allowed_hosts:
        raise ValueError("capture.allowed_hosts không được để trống")
    target_url = str(raw.get("target_url", "https://ik.playfun.vn/play-game"))
    if not is_allowed_url(target_url, allowed_hosts):
        raise ValueError("target_url phải là HTTP(S) thuộc capture.allowed_hosts")
    capture = CaptureSettings(
        allowed_hosts=allowed_hosts,
        max_body_bytes=int(capture_raw.get("max_body_bytes", 131_072)),
        max_text_chars=int(capture_raw.get("max_text_chars", 6_000)),
        capture_response_bodies=bool(capture_raw.get("capture_response_bodies", False)),
        network_capture_enabled=bool(capture_raw.get("network_capture_enabled", False)),
        snapshot_retention=max(1, int(capture_raw.get("snapshot_retention", 50))),
    )

    profiles: list[ProfileConfig] = []
    seen: set[str] = set()
    for item in raw.get("profiles", []):
        profile_id = slugify(str(item["id"]))
        if profile_id in seen:
            raise ValueError(f"Profile id bị trùng: {profile_id}")
        seen.add(profile_id)
        mode = ProfileMode(str(item.get("mode", "managed")))
        profile = ProfileConfig(
            id=profile_id,
            name=str(item.get("name", profile_id)),
            mode=mode,
            user_data_dir=_resolve(root, item.get("user_data_dir")),
            cdp_url=item.get("cdp_url"),
            enabled=bool(item.get("enabled", True)),
        )
        if mode == ProfileMode.MANAGED and profile.user_data_dir is None:
            profile.user_data_dir = (root / "data" / "profiles" / profile_id).resolve()
        if mode == ProfileMode.CDP and not profile.cdp_url:
            raise ValueError(f"Profile CDP {profile_id} thiếu cdp_url")
        profiles.append(profile)

    return AppConfig(
        root=root,
        source=source,
        target_url=target_url,
        data_dir=_resolve(root, str(raw.get("data_dir", "data"))) or root / "data",
        browser=browser,
        capture=capture,
        profiles=profiles,
    )


def save_config(config: AppConfig) -> None:
    raw = {
        "target_url": config.target_url,
        "data_dir": _relative_or_absolute(config.root, config.data_dir),
        "browser": {
            "chrome_executable": config.browser.chrome_executable,
            "headless": config.browser.headless,
            "app_mode": config.browser.app_mode,
            "profile_title": config.browser.profile_title,
            "low_memory_mode": config.browser.low_memory_mode,
            "auto_resize": config.browser.auto_resize,
            "viewport": {
                "width": config.browser.viewport_width,
                "height": config.browser.viewport_height,
            },
            "windows_per_row": min(6, max(2, int(config.browser.windows_per_row))),
            "slow_mo_ms": config.browser.slow_mo_ms,
            "startup_timeout_ms": config.browser.startup_timeout_ms,
        },
        "capture": {
            "allowed_hosts": list(config.capture.allowed_hosts),
            "max_body_bytes": config.capture.max_body_bytes,
            "max_text_chars": config.capture.max_text_chars,
            "capture_response_bodies": config.capture.capture_response_bodies,
            "network_capture_enabled": config.capture.network_capture_enabled,
            "snapshot_retention": config.capture.snapshot_retention,
        },
        "profiles": [
            {
                "id": profile.id,
                "name": profile.name,
                "mode": profile.mode.value,
                "user_data_dir": _relative_or_absolute(config.root, profile.user_data_dir),
                "cdp_url": profile.cdp_url,
                "enabled": profile.enabled,
            }
            for profile in config.profiles
        ],
    }
    config.source.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_data_dirs(config: AppConfig) -> None:
    paths = [
        config.data_dir,
        config.data_dir / "profiles",
        config.data_dir / "snapshots",
        config.data_dir / "screenshots",
        config.data_dir / "logs",
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
