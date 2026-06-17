from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import select

from database.db import instance_to_dict, session_scope
from database.models import Plate
from database.plate_assignments_db import list_active_assignments_for_plate
from helpers.plate_normalize import normalize_plate
from helpers.uuid_utils import parse_uuid


def _active_plates(stmt):
    return stmt.where(Plate.deleted_at.is_(None))


def find_plate_by_normalized(plate_normalized: str) -> dict | None:
    norm = normalize_plate(plate_normalized) or plate_normalized
    with session_scope() as session:
        row = session.scalar(
            _active_plates(select(Plate).where(Plate.plate_normalized == norm))
        )
        return _plate_dict(row) if row else None


def get_plate_by_id(plate_id: UUID | str) -> dict | None:
    pid = parse_uuid(plate_id)
    if pid is None:
        return None
    with session_scope() as session:
        row = session.get(Plate, pid)
        if row is None or row.deleted_at is not None:
            return None
        return _plate_dict(row)


def insert_plate(
    *,
    plate_number: str,
    plate_color: str = "default",
    is_guest: bool = False,
    guest_expires_at: datetime.datetime | None = None,
) -> tuple[UUID, str] | None:
    normalized = normalize_plate(plate_number)
    if not normalized:
        return None
    with session_scope() as session:
        existing = session.scalar(
            _active_plates(select(Plate).where(Plate.plate_normalized == normalized))
        )
        if existing is not None:
            return None
        row = Plate(
            plate_number=plate_number.strip(),
            plate_normalized=normalized,
            plate_color=plate_color or "default",
            is_guest=is_guest,
            guest_expires_at=guest_expires_at,
        )
        session.add(row)
        session.flush()
        return row.id, normalized


def get_or_create_plate(
    *,
    plate_number: str,
    plate_color: str = "default",
    is_guest: bool = False,
    guest_expires_at: datetime.datetime | None = None,
) -> tuple[dict, bool]:
    """Return (plate_dict, created). Reuses active plate when normalized value exists."""
    normalized = normalize_plate(plate_number)
    if not normalized:
        raise ValueError("invalid plate")
    with session_scope() as session:
        row = session.scalar(
            _active_plates(select(Plate).where(Plate.plate_normalized == normalized))
        )
        if row is not None:
            return _plate_dict(row), False
        row = Plate(
            plate_number=plate_number.strip(),
            plate_normalized=normalized,
            plate_color=plate_color or "default",
            is_guest=is_guest,
            guest_expires_at=guest_expires_at,
        )
        session.add(row)
        session.flush()
        return _plate_dict(row), True


def soft_delete_plate(*, plate_id: UUID | str | None = None, plate_number: str | None = None) -> bool:
    with session_scope() as session:
        row = None
        if plate_id is not None:
            pid = parse_uuid(plate_id)
            if pid is None:
                return False
            row = session.get(Plate, pid)
        elif plate_number:
            norm = normalize_plate(plate_number)
            row = session.scalar(
                _active_plates(select(Plate).where(Plate.plate_normalized == norm))
            )
        if row is None or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        return True


def list_expired_guest_plate_ids() -> list[UUID]:
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope() as session:
        rows = session.execute(
            _active_plates(
                select(Plate.id).where(
                    Plate.is_guest.is_(True),
                    Plate.guest_expires_at.is_not(None),
                    Plate.guest_expires_at <= now,
                )
            )
        ).scalars().all()
        return list(rows)


def get_plate_detail_by_normalized(plate_normalized: str) -> dict | None:
    """Plate record plus active vehicle assignments (newest first)."""
    plate = find_plate_by_normalized(plate_normalized)
    if not plate:
        return None
    assignments = list_active_assignments_for_plate(plate["id"])
    vehicles = []
    for item in assignments:
        vehicle = item.get("vehicle") or {}
        vehicles.append(
            {
                "assignment_id": item.get("id"),
                "vehicle_id": vehicle.get("id"),
                "is_primary": bool(item.get("is_primary")),
                "owner_name": vehicle.get("owner_name"),
                "owner_lastname": vehicle.get("owner_lastname"),
                "car_model": vehicle.get("car_model"),
                "vehicle_class": vehicle.get("vehicle_class"),
                "parking_spot": vehicle.get("parking_spot"),
                "created_at": (
                    item["created_at"].isoformat()
                    if item.get("created_at") is not None and hasattr(item["created_at"], "isoformat")
                    else item.get("created_at")
                ),
            }
        )
    return {"plate": plate, "vehicles": vehicles}


def _plate_dict(row: Plate) -> dict:
    d = instance_to_dict(row)
    if d.get("guest_expires_at") is not None and hasattr(d["guest_expires_at"], "isoformat"):
        d["guest_expires_at"] = d["guest_expires_at"].isoformat()
    return d
