from __future__ import annotations

import datetime
import os

from sqlalchemy import select

from database.db import instance_to_dict, session_scope
from database.models import Setting

# Global keys editable from admin panel (values stored as JSON-compatible dicts).
BOOTSTRAP_KEYS = (
    "PLATE_OCR_MIN_CONFIDENCE",
    "CAMERA_FRAME_INTERVAL_SECONDS",
    "PARKING_LOG_COOLDOWN_SECONDS",
    "light_profile_global",
)


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


def list_settings() -> list[dict]:
    with session_scope() as session:
        rows = session.execute(select(Setting).order_by(Setting.key.asc())).scalars().all()
        return [instance_to_dict(row) for row in rows]


def bootstrap_settings_from_env() -> None:
    """Copy selected env defaults into settings when missing."""
    defaults: dict[str, dict] = {
        "PLATE_OCR_MIN_CONFIDENCE": {"value": os.environ.get("PLATE_OCR_MIN_CONFIDENCE", "0.45")},
        "CAMERA_FRAME_INTERVAL_SECONDS": {
            "value": os.environ.get("CAMERA_FRAME_INTERVAL_SECONDS", "1.0")
        },
        "PARKING_LOG_COOLDOWN_SECONDS": {
            "value": os.environ.get("PARKING_LOG_COOLDOWN_SECONDS", "600")
        },
        "light_profile_global": {"value": "normal"},
    }
    for key, value in defaults.items():
        if get_setting(key) is None:
            set_setting(key, value)
