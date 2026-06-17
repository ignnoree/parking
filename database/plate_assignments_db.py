from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import select

from database.db import instance_to_dict, session_scope
from database.models import PlateAssignment, Vehicle
from helpers.uuid_utils import parse_uuid


def _active_assignments(stmt):
    return stmt.where(PlateAssignment.deleted_at.is_(None))


def create_assignment(
    plate_id: UUID | str,
    vehicle_id: UUID | str,
    *,
    is_primary: bool = False,
) -> UUID | None:
    pid = parse_uuid(plate_id)
    vid = parse_uuid(vehicle_id)
    if pid is None or vid is None:
        return None
    with session_scope() as session:
        exists = session.scalar(
            _active_assignments(
                select(PlateAssignment.id).where(
                    PlateAssignment.plate_id == pid,
                    PlateAssignment.vehicle_id == vid,
                )
            )
        )
        if exists is not None:
            return None
        if is_primary:
            for row in session.execute(
                _active_assignments(select(PlateAssignment).where(PlateAssignment.plate_id == pid))
            ).scalars():
                row.is_primary = False
        else:
            has_any = session.scalar(
                _active_assignments(
                    select(PlateAssignment.id).where(PlateAssignment.plate_id == pid)
                )
            )
            is_primary = has_any is None
        row = PlateAssignment(plate_id=pid, vehicle_id=vid, is_primary=is_primary)
        session.add(row)
        session.flush()
        return row.id


def list_active_assignments_for_plate(plate_id: UUID | str) -> list[dict]:
    pid = parse_uuid(plate_id)
    if pid is None:
        return []
    with session_scope() as session:
        rows = session.execute(
            _active_assignments(
                select(PlateAssignment, Vehicle)
                .join(Vehicle, Vehicle.id == PlateAssignment.vehicle_id)
                .where(
                    PlateAssignment.plate_id == pid,
                    Vehicle.deleted_at.is_(None),
                )
                .order_by(PlateAssignment.is_primary.desc(), PlateAssignment.created_at.desc())
            )
        ).all()
        out: list[dict] = []
        for assignment, vehicle in rows:
            item = instance_to_dict(assignment)
            item["vehicle"] = instance_to_dict(vehicle)
            out.append(item)
        return out


def get_primary_vehicle_for_plate(plate_id: UUID | str) -> dict | None:
    assignments = list_active_assignments_for_plate(plate_id)
    if not assignments:
        return None
    primary = next((a for a in assignments if a.get("is_primary")), assignments[0])
    return primary.get("vehicle")


def set_primary_vehicle_for_plate(plate_id: UUID | str, vehicle_id: UUID | str) -> bool:
    """Mark one linked vehicle as the primary car for this plate."""
    pid = parse_uuid(plate_id)
    vid = parse_uuid(vehicle_id)
    if pid is None or vid is None:
        return False
    with session_scope() as session:
        target = session.scalar(
            _active_assignments(
                select(PlateAssignment).where(
                    PlateAssignment.plate_id == pid,
                    PlateAssignment.vehicle_id == vid,
                )
            )
        )
        if target is None:
            return False
        for row in session.execute(
            _active_assignments(select(PlateAssignment).where(PlateAssignment.plate_id == pid))
        ).scalars():
            row.is_primary = row.id == target.id
        return True


def soft_delete_assignments_for_vehicle(vehicle_id: UUID | str) -> int:
    vid = parse_uuid(vehicle_id)
    if vid is None:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    with session_scope() as session:
        rows = session.execute(
            _active_assignments(select(PlateAssignment).where(PlateAssignment.vehicle_id == vid))
        ).scalars().all()
        for row in rows:
            row.deleted_at = now
        return len(rows)
