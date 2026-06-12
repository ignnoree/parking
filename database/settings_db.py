from __future__ import annotations

import datetime

from sqlalchemy import select

from database.db import instance_to_dict, session_scope
from database.models import Setting

# Keys editable from admin panel (values stored as JSON-compatible dicts).
BOOTSTRAP_KEYS = (
    "CAMERA_FRAME_INTERVAL_SECONDS",
    "PARKING_LOG_COOLDOWN_SECONDS",
    "light_profile_global",
)

# Seeded into PostgreSQL on first start; runtime reads DB only (not .env).
DEFAULT_SETTINGS: dict[str, dict] = {
    "CAMERA_FRAME_INTERVAL_SECONDS": {"value": "1.0"},
    "PARKING_LOG_COOLDOWN_SECONDS": {"value": "600"},
    "light_profile_global": {"value": "normal"},
}


def get_setting(key: str) -> dict | None:
    with session_scope() as session:
        row = session.get(Setting, key)
        return row.value if row else None


def set_setting(key: str, value: dict | None) -> None:
    with session_scope() as session:
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
            session.add(row)
        else:
            row.value = value
            row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    try:
        from helpers.runtime_settings import invalidate_runtime_settings_cache

        invalidate_runtime_settings_cache()
    except ImportError:
        pass


def list_settings() -> list[dict]:
    with session_scope() as session:
        rows = session.execute(select(Setting).order_by(Setting.key.asc())).scalars().all()
        return [instance_to_dict(row) for row in rows]


def bootstrap_default_settings() -> None:
    """Insert code defaults for settings rows that do not exist yet."""
    for key, value in DEFAULT_SETTINGS.items():
        if get_setting(key) is None:
            set_setting(key, value)


def bootstrap_settings_from_env() -> None:
    """Backward-compatible alias; env is no longer read."""
    bootstrap_default_settings()
