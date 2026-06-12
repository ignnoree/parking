"""Emit software_logs lighting_warning when glare/low-light scans fail repeatedly."""

from __future__ import annotations

import datetime
import threading

from database.logs_db import log_software_event

_lock = threading.Lock()
_empty_streak = 0
_last_warning_at: datetime.datetime | None = None

WARNING_STREAK = 15
WARNING_COOLDOWN_SECONDS = 300


def note_plate_scan(*, light_profile: str, plates_logged: int) -> None:
    global _empty_streak, _last_warning_at
    profile = (light_profile or "normal").strip().lower()
    if plates_logged > 0:
        with _lock:
            _empty_streak = 0
        return
    if profile not in {"high_glare", "low_light"}:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _empty_streak += 1
        if _empty_streak < WARNING_STREAK:
            return
        if _last_warning_at and (now - _last_warning_at).total_seconds() < WARNING_COOLDOWN_SECONDS:
            _empty_streak = 0
            return
        _empty_streak = 0
        _last_warning_at = now
    log_software_event(
        level="WARN",
        event="lighting_warning",
        module="helpers.lighting_monitor",
        message=f"Repeated scans with no plates under light_profile={profile}",
        metadata=f"streak_threshold={WARNING_STREAK}",
    )
