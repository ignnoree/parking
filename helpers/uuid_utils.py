from __future__ import annotations

from uuid import UUID


def parse_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None
