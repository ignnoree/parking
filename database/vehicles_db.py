from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import func, select

from database.db import instance_to_dict, session_scope
from database.models import Plate, PlateAssignment, Vehicle
from helpers.plate_normalize import normalize_plate
from helpers.uuid_utils import parse_uuid


def _active_vehicles(stmt):
    return stmt.where(Vehicle.deleted_at.is_(None))


def insert_vehicle(
    *,
    owner_name: str | None = None,
    owner_lastname: str | None = None,
    car_model: str | None = None,
    door_number: str | None = None,
    floor_number: str | None = None,
    parking_spot: str | None = None,
    vehicle_class: str = "car",
    reference_image_path: str | None = None,
    metadata: dict | None = None,
) -> UUID | None:
    with session_scope() as session:
        row = Vehicle(
            owner_name=owner_name,
            owner_lastname=owner_lastname,
            car_model=car_model,
            door_number=door_number,
            floor_number=floor_number,
            parking_spot=parking_spot,
            vehicle_class=vehicle_class or "car",
            reference_image_path=reference_image_path,
            metadata_=metadata,
        )
        session.add(row)
        session.flush()
        return row.id


def soft_delete_vehicle(*, vehicle_id: UUID | str | None = None, plate_number: str | None = None) -> bool:
    with session_scope() as session:
        row = None
        if vehicle_id is not None:
            vid = parse_uuid(vehicle_id)
            if vid is None:
                return False
            row = session.get(Vehicle, vid)
        elif plate_number:
            norm = normalize_plate(plate_number)
            row = session.scalar(
                _active_vehicles(
                    select(Vehicle)
                    .join(PlateAssignment, PlateAssignment.vehicle_id == Vehicle.id)
                    .join(Plate, Plate.id == PlateAssignment.plate_id)
                    .where(
                        Plate.plate_normalized == norm,
                        Plate.deleted_at.is_(None),
                        PlateAssignment.deleted_at.is_(None),
                    )
                    .order_by(PlateAssignment.is_primary.desc())
                )
            )
        if row is None or row.deleted_at is not None:
            return False
        now = datetime.datetime.now(datetime.timezone.utc)
        row.deleted_at = now
        for assignment in session.execute(
            select(PlateAssignment).where(
                PlateAssignment.vehicle_id == row.id,
                PlateAssignment.deleted_at.is_(None),
            )
        ).scalars():
            assignment.deleted_at = now
        return True


def _vehicles_filter_stmt(
    *,
    plate: str | None = None,
    owner: str | None = None,
    is_guest: bool | None = None,
):
    stmt = _active_vehicles(select(Vehicle).order_by(Vehicle.created_at.desc()))
    if plate:
        norm = normalize_plate(plate)
        stmt = (
            stmt.join(PlateAssignment, PlateAssignment.vehicle_id == Vehicle.id)
            .join(Plate, Plate.id == PlateAssignment.plate_id)
            .where(
                Plate.plate_normalized.ilike(f"%{norm}%"),
                Plate.deleted_at.is_(None),
                PlateAssignment.deleted_at.is_(None),
            )
        )
    if owner:
        term = f"%{owner.strip()}%"
        stmt = stmt.where(
            (Vehicle.owner_name.ilike(term)) | (Vehicle.owner_lastname.ilike(term))
        )
    if is_guest is not None:
        if plate is None:
            stmt = stmt.join(PlateAssignment, PlateAssignment.vehicle_id == Vehicle.id).join(
                Plate, Plate.id == PlateAssignment.plate_id
            )
        stmt = stmt.where(
            Plate.is_guest == is_guest,
            Plate.deleted_at.is_(None),
            PlateAssignment.deleted_at.is_(None),
        )
    return stmt.distinct()


def count_vehicles(
    *,
    plate: str | None = None,
    owner: str | None = None,
    is_guest: bool | None = None,
) -> int:
    with session_scope() as session:
        subq = _vehicles_filter_stmt(plate=plate, owner=owner, is_guest=is_guest).subquery()
        return int(session.scalar(select(func.count()).select_from(subq)) or 0)


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
        vehicle_rows = session.execute(stmt).scalars().all()
        return [_vehicle_with_plates(session, row) for row in vehicle_rows]


def get_vehicles_by_ids(vehicle_ids: list) -> dict[str, dict]:
    if not vehicle_ids:
        return {}
    parsed: list[UUID] = []
    for vid in vehicle_ids:
        uid = parse_uuid(vid)
        if uid is not None:
            parsed.append(uid)
    if not parsed:
        return {}
    with session_scope() as session:
        rows = session.execute(select(Vehicle).where(Vehicle.id.in_(parsed))).scalars().all()
        return {str(row.id): _vehicle_with_plates(session, row) for row in rows}


def _vehicle_with_plates(session, vehicle: Vehicle) -> dict:
    d = instance_to_dict(vehicle)
    plates = session.execute(
        select(Plate, PlateAssignment)
        .join(PlateAssignment, PlateAssignment.plate_id == Plate.id)
        .where(
            PlateAssignment.vehicle_id == vehicle.id,
            PlateAssignment.deleted_at.is_(None),
            Plate.deleted_at.is_(None),
        )
        .order_by(PlateAssignment.is_primary.desc(), PlateAssignment.created_at.desc())
    ).all()
    plate_rows: list[dict] = []
    for plate, assignment in plates:
        p = instance_to_dict(plate)
        if p.get("guest_expires_at") is not None and hasattr(p["guest_expires_at"], "isoformat"):
            p["guest_expires_at"] = p["guest_expires_at"].isoformat()
        p["is_primary"] = assignment.is_primary
        plate_rows.append(p)
    d["plates"] = plate_rows
    primary = plate_rows[0] if plate_rows else None
    d["plate_number"] = primary["plate_number"] if primary else None
    d["plate_normalized"] = primary["plate_normalized"] if primary else None
    d["plate_color"] = primary.get("plate_color") if primary else None
    d["is_guest"] = bool(primary.get("is_guest")) if primary else False
    if primary and primary.get("guest_expires_at"):
        d["guest_expires_at"] = primary["guest_expires_at"]
    return d
