import datetime
import logging
from uuid import UUID

from sqlalchemy import func, or_, select

from database.db import session_scope, instance_to_dict
from database.models import ParkingLog, Plate, SoftwareLog, Vehicle
from database.enrollment_db import resolve_plate_lookup
from helpers.plate_normalize import normalize_plate
from helpers.uuid_utils import parse_uuid

_logger = logging.getLogger(__name__)


def _actor_from_request() -> tuple[UUID, str] | None:
    """Return (admin_id, username) when called inside an authenticated Flask request."""
    try:
        from flask import has_request_context

        if not has_request_context():
            return None
        from helpers.rbac import get_current_admin

        admin = get_current_admin()
        if not admin:
            return None
        admin_id = parse_uuid(admin["id"])
        if admin_id is None:
            return None
        return admin_id, str(admin.get("username") or "")
    except Exception:
        return None


def log_software_event(
    level: str,
    message: str,
    event: str | None = None,
    module: str | None = None,
    metadata: str | None = None,
    *,
    admin_id: UUID | str | None = None,
    admin_username: str | None = None,
) -> UUID | None:
    """Mirror to terminal, then persist to software_logs (never raises to caller)."""
    if admin_id is None and not admin_username:
        actor = _actor_from_request()
        if actor is not None:
            admin_id, admin_username = actor
    elif admin_id is not None:
        admin_id = parse_uuid(admin_id)

    py_level = getattr(logging, level.upper(), logging.INFO)
    actor_suffix = f" admin={admin_username!r}" if admin_username else ""
    _logger.log(
        py_level,
        "[%s] %s%s%s",
        event or "software",
        message,
        f" ({metadata})" if metadata else "",
        actor_suffix,
    )
    try:
        with session_scope() as session:
            row = SoftwareLog(
                level=level.upper(),
                event=event,
                module=module,
                message=message,
                metadata_=metadata,
                admin_id=admin_id,
                admin_username=admin_username,
            )
            session.add(row)
            session.flush()
            return row.id
    except Exception:
        _logger.exception("Failed to persist software log event=%s", event)
        return None


def log_parking_event(
    *,
    plate_normalized: str,
    direction: str,
    match_status: str,
    plate_number: str | None = None,
    plate_id: UUID | str | None = None,
    vehicle_id: UUID | str | None = None,
    is_guest: bool = False,
    confidence: float | None = None,
    snapshot_path: str | None = None,
    details: str | None = None,
) -> UUID | None:
    pid = parse_uuid(plate_id) if plate_id is not None else None
    vid = parse_uuid(vehicle_id) if vehicle_id is not None else None
    with session_scope() as session:
        row = ParkingLog(
            plate_normalized=plate_normalized,
            plate_number=plate_number,
            direction=direction,
            match_status=match_status,
            plate_id=pid,
            vehicle_id=vid,
            is_guest=is_guest,
            confidence=confidence,
            snapshot_path=snapshot_path,
            details=details,
        )
        session.add(row)
        session.flush()
        return row.id


def count_parking_logs(*, include_deleted_vehicles: bool = False, **filters) -> int:
    with session_scope() as session:
        stmt = _apply_parking_filters(
            select(func.count()).select_from(ParkingLog),
            include_deleted_vehicles=include_deleted_vehicles,
            **filters,
        )
        return int(session.scalar(stmt) or 0)


def list_parking_logs(
    *,
    limit: int = 50,
    offset: int = 0,
    include_deleted_vehicles: bool = False,
    **filters,
) -> list[dict]:
    with session_scope() as session:
        stmt = _apply_parking_filters(
            select(ParkingLog).order_by(ParkingLog.logged_at.desc()),
            include_deleted_vehicles=include_deleted_vehicles,
            **filters,
        )
        stmt = stmt.limit(limit).offset(offset)
        rows = session.execute(stmt).scalars().all()
        out = []
        for row in rows:
            d = instance_to_dict(row)
            if d.get("logged_at") is not None and hasattr(d["logged_at"], "isoformat"):
                d["logged_at"] = d["logged_at"].isoformat()
            out.append(d)
        return out


def count_software_logs(**filters) -> int:
    with session_scope() as session:
        stmt = _apply_software_filters(select(func.count()).select_from(SoftwareLog), **filters)
        return int(session.scalar(stmt) or 0)


def soft_delete_parking_log(log_id: UUID | str) -> bool:
    lid = parse_uuid(log_id)
    if lid is None:
        return False
    with session_scope() as session:
        row = session.get(ParkingLog, lid)
        if row is None or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        return True


def list_software_logs(*, limit: int = 50, offset: int = 0, **filters) -> list[dict]:
    with session_scope() as session:
        stmt = _apply_software_filters(
            select(SoftwareLog).order_by(SoftwareLog.logged_at.desc()),
            **filters,
        )
        stmt = stmt.limit(limit).offset(offset)
        out = []
        for row in session.execute(stmt).scalars().all():
            d = instance_to_dict(row)
            if d.get("logged_at") is not None and hasattr(d["logged_at"], "isoformat"):
                d["logged_at"] = d["logged_at"].isoformat()
            out.append(d)
        return out


def update_parking_log_plate(log_id: UUID | str, new_plate: str) -> dict | None:
    """
    Correct the plate on a parking log. Re-resolves vehicle_id, match_status, and is_guest
    against the vehicles table. Returns a result dict, or None if log not found / plate invalid.
    Does NOT write the audit software log — caller is responsible for that.
    """
    lid = parse_uuid(log_id)
    if lid is None:
        return None
    normalized = normalize_plate(new_plate)
    if not normalized:
        return None
    with session_scope() as session:
        row = session.get(ParkingLog, lid)
        if row is None or row.deleted_at is not None:
            return None
        old_plate_number = row.plate_number
        old_plate_normalized = row.plate_normalized
        vehicle = resolve_plate_lookup(normalized)
        row.plate_number = new_plate.strip()
        row.plate_normalized = normalized
        if vehicle is not None:
            row.plate_id = parse_uuid(vehicle["plate_id"])
            row.vehicle_id = parse_uuid(vehicle.get("vehicle_id")) if vehicle.get("vehicle_id") else None
            row.match_status = "registered"
            row.is_guest = bool(vehicle.get("is_guest"))
        else:
            row.plate_id = None
            row.vehicle_id = None
            row.match_status = "unregistered"
            row.is_guest = False
        return {
            "old_plate_number": old_plate_number,
            "old_plate_normalized": old_plate_normalized,
            "new_plate_number": new_plate.strip(),
            "new_plate_normalized": normalized,
            "vehicle_found": vehicle is not None,
        }


def _deleted_plates_subquery():
    return select(Plate.plate_normalized).where(Plate.deleted_at.is_not(None))


def _parse_logged_at_filter(value: str | None):
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _apply_parking_filters(
    stmt,
    *,
    direction: str | None = None,
    match_status: str | None = None,
    plate: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    include_deleted_vehicles: bool = False,
):
    stmt = stmt.where(ParkingLog.deleted_at.is_(None))
    if not include_deleted_vehicles:
        deleted_plates = _deleted_plates_subquery()
        stmt = stmt.where(
            or_(
                ParkingLog.plate_id.is_(None),
                ParkingLog.plate_id.in_(select(Plate.id).where(Plate.deleted_at.is_(None))),
            )
        )
        stmt = stmt.where(
            or_(
                ParkingLog.vehicle_id.is_(None),
                ParkingLog.vehicle_id.in_(
                    select(Vehicle.id).where(Vehicle.deleted_at.is_(None))
                ),
            )
        )
        stmt = stmt.where(~ParkingLog.plate_normalized.in_(deleted_plates))
    if direction:
        stmt = stmt.where(ParkingLog.direction == direction)
    if match_status:
        stmt = stmt.where(ParkingLog.match_status == match_status)
    if plate:
        stmt = stmt.where(ParkingLog.plate_normalized.ilike(f"%{plate.strip()}%"))
    from_dt = _parse_logged_at_filter(from_date)
    if from_dt is not None:
        stmt = stmt.where(ParkingLog.logged_at >= from_dt)
    to_dt = _parse_logged_at_filter(to_date)
    if to_dt is not None:
        stmt = stmt.where(ParkingLog.logged_at <= to_dt)
    return stmt


def _apply_software_filters(
    stmt,
    *,
    level: str | None = None,
    event: str | None = None,
    module: str | None = None,
):
    if level:
        stmt = stmt.where(SoftwareLog.level == level.upper())
    if event:
        stmt = stmt.where(SoftwareLog.event.ilike(f"%{event.strip()}%"))
    if module:
        stmt = stmt.where(SoftwareLog.module.ilike(f"%{module.strip()}%"))
    return stmt
