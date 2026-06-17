from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import select

from database.db import session_scope
from database.models import Plate, PlateAssignment, Vehicle
from database.plate_assignments_db import get_primary_vehicle_for_plate
from database.plates_db import find_plate_by_normalized
from helpers.plate_normalize import normalize_plate


def resolve_plate_lookup(plate_normalized: str) -> dict | None:
    """
    Resolve OCR plate to registered plate + primary vehicle (if any).
    Returns a merged dict for pipeline/logging, or None when plate is not registered.
    """
    norm = normalize_plate(plate_normalized) or plate_normalized
    plate = find_plate_by_normalized(norm)
    if not plate:
        return None
    if _plate_guest_expired(plate):
        return None
    vehicle = get_primary_vehicle_for_plate(plate["id"])
    out = {
        "plate_id": plate["id"],
        "plate_number": plate["plate_number"],
        "plate_normalized": plate["plate_normalized"],
        "plate_color": plate.get("plate_color"),
        "is_guest": bool(plate.get("is_guest")),
    }
    if vehicle:
        out["vehicle_id"] = vehicle["id"]
        out["owner_name"] = vehicle.get("owner_name")
        out["owner_lastname"] = vehicle.get("owner_lastname")
        out["car_model"] = vehicle.get("car_model")
        out["parking_spot"] = vehicle.get("parking_spot")
        out["floor_number"] = vehicle.get("floor_number")
        out["door_number"] = vehicle.get("door_number")
        out["vehicle_class"] = vehicle.get("vehicle_class")
    else:
        out["vehicle_id"] = None
    return out


def find_vehicle_by_normalized(plate_normalized: str) -> dict | None:
    """Backward-compatible lookup used by OCR pipeline (plate + primary vehicle)."""
    return resolve_plate_lookup(plate_normalized)


def enroll_vehicle(
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
) -> tuple[UUID, UUID, str] | None:
    """Register a vehicle and link it to a plate (create plate if needed)."""
    normalized = normalize_plate(plate_number)
    if not normalized:
        return None
    with session_scope() as session:
        plate = session.scalar(
            select(Plate).where(
                Plate.plate_normalized == normalized,
                Plate.deleted_at.is_(None),
            )
        )
        if plate is None:
            plate = Plate(
                plate_number=plate_number.strip(),
                plate_normalized=normalized,
                plate_color=plate_color or "default",
                is_guest=is_guest,
                guest_expires_at=guest_expires_at,
            )
            session.add(plate)
            session.flush()
        vehicle = Vehicle(
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
        session.add(vehicle)
        session.flush()
        exists = session.scalar(
            select(PlateAssignment.id).where(
                PlateAssignment.plate_id == plate.id,
                PlateAssignment.vehicle_id == vehicle.id,
                PlateAssignment.deleted_at.is_(None),
            )
        )
        if exists is not None:
            return None
        for row in session.execute(
            select(PlateAssignment).where(
                PlateAssignment.plate_id == plate.id,
                PlateAssignment.deleted_at.is_(None),
            )
        ).scalars():
            row.is_primary = False
        assignment = PlateAssignment(
            plate_id=plate.id,
            vehicle_id=vehicle.id,
            is_primary=True,
        )
        session.add(assignment)
        session.flush()
        return vehicle.id, plate.id, normalized


def _plate_guest_expired(plate: dict) -> bool:
    if not plate.get("is_guest"):
        return False
    exp = plate.get("guest_expires_at")
    if exp is None:
        return False
    if isinstance(exp, str):
        exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
    else:
        exp_dt = exp
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
    return exp_dt <= datetime.datetime.now(datetime.timezone.utc)
