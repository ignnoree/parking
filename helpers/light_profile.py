"""Resolve effective light profile: per-camera override, else DB global default."""

from __future__ import annotations

from helpers.runtime_settings import global_light_profile

_VALID = frozenset({"normal", "high_glare", "low_light"})


def resolve_light_profile(camera_profile: str | None) -> str:
    raw = (camera_profile or "").strip().lower()
    if raw in _VALID:
        return raw
    return global_light_profile()
