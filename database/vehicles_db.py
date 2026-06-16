from __future__ import annotations

import datetime

from sqlalchemy import func, select

from database.db import session_scope, instance_to_dict
from database.models import Vehicle
from helpers.plate_normalize import normalize_plate


def _active_filter(stmt):
    return stmt.where(Vehicle.deleted_at.is_(None))


def find_vehicle_by_normalized(plate_normalized: str) -> dict | None:
    with session_scope() as session:
        row = session.scalar(
            _active_filter(select(Vehicle).where(Vehicle.plate_normalized == plate_normalized))
        )
        return instance_to_dict(row) if row else None


def insert_vehicle(
    *,
    plate_number: str,
    owner_name: str | None = None,
    owner_lastname: str | None = None,
    car_model: str | None = None,
    door_number: str | None = None,
    floor_number: str | None = None,
    parking_spot: str | None = None,
    plate_color: str = "default",
    vehicle_class: str = "car",
    is_guest: bool = False,
    guest_expires_at: datetime.datetime | None = None,
    reference_image_path: str | None = None,
    metadata: dict | None = None,
) -> tuple[int, str] | None:
    normalized = normalize_plate(plate_number)
    if not normalized:
        return None
    with session_scope() as session:
        clash = session.scalar(
            _active_filter(select(Vehicle.id).where(Vehicle.plate_normalized == normalized))
        )
        if clash is not None:
            return None
        row = Vehicle(
            plate_number=plate_number.strip(),
            plate_normalized=normalized,
            owner_name=owner_name,
            owner_lastname=owner_lastname,
            car_model=car_model,
            door_number=door_number,
            floor_number=floor_number,
            parking_spot=parking_spot,
            plate_color=plate_color or "default",
            vehicle_class=vehicle_class or "car",
            is_guest=is_guest,
            guest_expires_at=guest_expires_at,
            reference_image_path=reference_image_path,
            metadata_=metadata,
        )
        session.add(row)
        session.flush()
        return row.id, normalized


def soft_delete_vehicle(*, vehicle_id: int | None = None, plate_number: str | None = None) -> bool:
    with session_scope() as session:
        row = None
        if vehicle_id is not None:
            row = session.get(Vehicle, vehicle_id)
        elif plate_number:
            norm = normalize_plate(plate_number)
            row = session.scalar(
                _active_filter(select(Vehicle).where(Vehicle.plate_normalized == norm))
            )
        if row is None or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        return True


def _vehicles_filter_stmt(
    *,
    plate: str | None = None,
    owner: str | None = None,
    is_guest: bool | None = None,
):
    stmt = _active_filter(select(Vehicle).order_by(Vehicle.id.desc()))
    if plate:
        norm = normalize_plate(plate)
        stmt = stmt.where(Vehicle.plate_normalized.ilike(f"%{norm}%"))
    if owner:
        term = f"%{owner.strip()}%"
        stmt = stmt.where(
            (Vehicle.owner_name.ilike(term)) | (Vehicle.owner_lastname.ilike(term))
        )
    if is_guest is not None:
        stmt = stmt.where(Vehicle.is_guest == is_guest)
    return stmt


def count_vehicles(
    *,
    plate: str | None = None,
    owner: str | None = None,
    is_guest: bool | None = None,
) -> int:
    with session_scope() as session:
        stmt = select(func.count()).select_from(Vehicle)
        stmt = _active_filter(stmt)
        if plate:
            norm = normalize_plate(plate)
            stmt = stmt.where(Vehicle.plate_normalized.ilike(f"%{norm}%"))
        if owner:
            term = f"%{owner.strip()}%"
            stmt = stmt.where(
                (Vehicle.owner_name.ilike(term)) | (Vehicle.owner_lastname.ilike(term))
            )
        if is_guest is not None:
            stmt = stmt.where(Vehicle.is_guest == is_guest)
        return int(session.scalar(stmt) or 0)


def list_vehicles(
    *,
    plate: str | None = None,
    owner: str | None = None,
    is_guest: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    with session_scope() as session:
        stmt = _vehicles_filter_stmt(plate=plate, owner=owner, is_guest=is_guest)
        stmt = stmt.limit(limit).offset(offset)
        rows = []
        for row in session.execute(stmt).scalars().all():
            d = instance_to_dict(row)
            if d.get("guest_expires_at") is not None and hasattr(d["guest_expires_at"], "isoformat"):
                d["guest_expires_at"] = d["guest_expires_at"].isoformat()
            rows.append(d)
        return rows


def get_vehicles_by_ids(vehicle_ids: list[int]) -> dict[int, dict]:
    """Fetch multiple vehicles by id in one query. Returns {id: vehicle_dict}."""
    if not vehicle_ids:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(Vehicle).where(Vehicle.id.in_(vehicle_ids))
        ).scalars().all()
        out: dict[int, dict] = {}
        for row in rows:
            d = instance_to_dict(row)
            if d.get("guest_expires_at") is not None and hasattr(d["guest_expires_at"], "isoformat"):
                d["guest_expires_at"] = d["guest_expires_at"].isoformat()
            out[d["id"]] = d
        return out


def list_expired_guest_vehicle_ids() -> list[int]:
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope() as session:
        rows = session.execute(
            _active_filter(
                select(Vehicle.id).where(
                    Vehicle.is_guest.is_(True),
                    Vehicle.guest_expires_at.is_not(None),
                    Vehicle.guest_expires_at <= now,
                )
            )
        ).scalars().all()
        return list(rows)
