import logging

from sqlalchemy import func, select

from database.db import session_scope, instance_to_dict
from database.models import ParkingLog, SoftwareLog

_logger = logging.getLogger(__name__)


def log_software_event(
    level: str,
    message: str,
    event: str | None = None,
    module: str | None = None,
    metadata: str | None = None,
) -> int | None:
    """Mirror to terminal, then persist to software_logs (never raises to caller)."""
    py_level = getattr(logging, level.upper(), logging.INFO)
    _logger.log(
        py_level,
        "[%s] %s%s",
        event or "software",
        message,
        f" ({metadata})" if metadata else "",
    )
    try:
        with session_scope() as session:
            row = SoftwareLog(
                level=level.upper(),
                event=event,
                module=module,
                message=message,
                metadata_=metadata,
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
    vehicle_id: int | None = None,
    is_guest: bool = False,
    confidence: float | None = None,
    snapshot_path: str | None = None,
    details: str | None = None,
) -> int:
    with session_scope() as session:
        row = ParkingLog(
            plate_normalized=plate_normalized,
            plate_number=plate_number,
            direction=direction,
            match_status=match_status,
            vehicle_id=vehicle_id,
            is_guest=is_guest,
            confidence=confidence,
            snapshot_path=snapshot_path,
            details=details,
        )
        session.add(row)
        session.flush()
        return row.id


def count_parking_logs(**filters) -> int:
    with session_scope() as session:
        stmt = _apply_parking_filters(select(func.count()).select_from(ParkingLog), **filters)
        return int(session.scalar(stmt) or 0)


def list_parking_logs(*, limit: int = 50, offset: int = 0, **filters) -> list[dict]:
    with session_scope() as session:
        stmt = _apply_parking_filters(select(ParkingLog).order_by(ParkingLog.logged_at.desc()), **filters)
        stmt = stmt.limit(limit).offset(offset)
        rows = session.execute(stmt).scalars().all()
        out = []
        for row in rows:
            d = instance_to_dict(row)
            if d.get("logged_at") is not None and hasattr(d["logged_at"], "isoformat"):
                d["logged_at"] = d["logged_at"].isoformat()
            out.append(d)
        return out


def _apply_parking_filters(stmt, *, direction: str | None = None, match_status: str | None = None, plate: str | None = None):
    stmt = stmt.where(ParkingLog.deleted_at.is_(None))
    if direction:
        stmt = stmt.where(ParkingLog.direction == direction)
    if match_status:
        stmt = stmt.where(ParkingLog.match_status == match_status)
    if plate:
        stmt = stmt.where(ParkingLog.plate_normalized.ilike(f"%{plate.strip()}%"))
    return stmt
