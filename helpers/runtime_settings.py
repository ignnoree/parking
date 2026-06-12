"""
Runtime configuration from PostgreSQL `settings` only.

On startup, `bootstrap_default_settings()` seeds missing rows with code defaults.
Admin PATCH /api/settings updates apply live (cached ~2s; cleared on write).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

from database.settings_db import DEFAULT_SETTINGS, get_setting

T = TypeVar("T")

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL_SECONDS = 2.0


def invalidate_runtime_settings_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(key: str, loader: Callable[[], T]) -> T:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]
    value = loader()
    with _lock:
        _cache[key] = (now, value)
    return value


def _unwrap_setting(row: dict | None) -> object | None:
    if not row:
        return None
    if "value" in row:
        return row["value"]
    return row


def _code_default(setting_key: str) -> object | None:
    row = DEFAULT_SETTINGS.get(setting_key)
    if not row:
        return None
    return row.get("value")


def get_runtime_str(setting_key: str, default: str) -> str:
    def load() -> str:
        raw = _unwrap_setting(get_setting(setting_key))
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        fallback = _code_default(setting_key)
        if fallback is not None and str(fallback).strip():
            return str(fallback).strip()
        return default

    return _cached(f"str:{setting_key}", load)


def get_runtime_float(setting_key: str, default: float) -> float:
    def load() -> float:
        raw = _unwrap_setting(get_setting(setting_key))
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        fallback = _code_default(setting_key)
        if fallback is not None:
            try:
                return float(fallback)
            except (TypeError, ValueError):
                pass
        return default

    return _cached(f"float:{setting_key}", load)


def get_runtime_int(setting_key: str, default: int) -> int:
    return int(get_runtime_float(setting_key, float(default)))


def camera_frame_interval_seconds() -> float:
    return max(0.5, get_runtime_float("CAMERA_FRAME_INTERVAL_SECONDS", 0.5))


def parking_log_cooldown_seconds() -> int:
    return max(0, get_runtime_int("PARKING_LOG_COOLDOWN_SECONDS", 600))


def global_light_profile() -> str:
    raw = get_runtime_str("light_profile_global", "normal").lower()
    if raw in {"normal", "high_glare", "low_light"}:
        return raw
    return "normal"
