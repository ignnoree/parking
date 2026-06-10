from __future__ import annotations

import datetime
import os

from sqlalchemy import func, select

from database.db import instance_to_dict, session_scope
from database.models import Camera

VALID_PROTOCOLS = ("rtsp", "http", "usb")
VALID_GATE_ROLES = ("entry", "exit")
VALID_LIGHT_PROFILES = ("normal", "high_glare", "low_light")


def infer_protocol(source: str) -> str:
    value = source.strip()
    if value.isdigit():
        return "usb"
    lower = value.lower()
    if lower.startswith("rtsp"):
        return "rtsp"
    if lower.startswith("http"):
        return "http"
    if os.path.isfile(value):
        return "http"
    return "http"


def parse_camera_source(protocol: str, source: str) -> int | str | None:
    raw = (source or "").strip()
    if not raw:
        return None
    if protocol == "usb":
        return int(raw) if raw.isdigit() else None
    return raw


def normalized_source_key(protocol: str, source: str) -> str | None:
    """Canonical key for duplicate source checks."""
    parsed = parse_camera_source(protocol, source)
    if parsed is None:
        return None
    if isinstance(parsed, int):
        return f"usb:{parsed}"
    text = str(parsed).strip()
    if os.path.isfile(text):
        return f"file:{os.path.normcase(os.path.abspath(text))}"
    return f"url:{text.lower()}"


def source_in_use(protocol: str, source: str, *, exclude_id: int | None = None) -> bool:
    key = normalized_source_key(protocol, source)
    if key is None:
        return False
    for row in list_cameras():
        if exclude_id is not None and int(row["id"]) == exclude_id:
            continue
        existing = normalized_source_key(str(row["protocol"]), str(row["source"]))
        if existing == key:
            return True
    return False


def default_frame_interval() -> float:
    return max(0.5, float(os.environ.get("CAMERA_FRAME_INTERVAL_SECONDS", "1.0")))


def list_cameras(*, enabled_only: bool = False) -> list[dict]:
    with session_scope() as session:
        stmt = select(Camera).order_by(Camera.id.asc())
        if enabled_only:
            stmt = stmt.where(Camera.is_enabled.is_(True))
        return [instance_to_dict(row) for row in session.execute(stmt).scalars().all()]


def get_camera_by_id(camera_id: int) -> dict | None:
    with session_scope() as session:
        row = session.get(Camera, camera_id)
        return instance_to_dict(row) if row else None


def insert_camera(
    *,
    name: str,
    protocol: str,
    source: str,
    gate_role: str = "entry",
    is_enabled: bool = True,
    frame_interval_seconds: float | None = None,
    light_profile: str = "normal",
) -> int | None:
    protocol = protocol.strip().lower()
    gate_role = gate_role.strip().lower()
    light_profile = light_profile.strip().lower()
    if protocol not in VALID_PROTOCOLS:
        return None
    if gate_role not in VALID_GATE_ROLES:
        return None
    if light_profile not in VALID_LIGHT_PROFILES:
        return None
    if not name.strip() or not source.strip():
        return None
    if parse_camera_source(protocol, source) is None:
        return None
    if source_in_use(protocol, source):
        return None

    with session_scope() as session:
        row = Camera(
            name=name.strip(),
            protocol=protocol,
            source=source.strip(),
            gate_role=gate_role,
            is_enabled=is_enabled,
            frame_interval_seconds=frame_interval_seconds,
            light_profile=light_profile,
        )
        session.add(row)
        session.flush()
        return row.id


def update_camera(camera_id: int, **fields) -> bool:
    allowed = {
        "name",
        "protocol",
        "source",
        "gate_role",
        "is_enabled",
        "frame_interval_seconds",
        "light_profile",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    if "protocol" in updates:
        protocol = str(updates["protocol"]).strip().lower()
        if protocol not in VALID_PROTOCOLS:
            return False
        updates["protocol"] = protocol
    if "gate_role" in updates:
        gate_role = str(updates["gate_role"]).strip().lower()
        if gate_role not in VALID_GATE_ROLES:
            return False
        updates["gate_role"] = gate_role
    if "light_profile" in updates:
        light_profile = str(updates["light_profile"]).strip().lower()
        if light_profile not in VALID_LIGHT_PROFILES:
            return False
        updates["light_profile"] = light_profile
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip()
        if not updates["name"]:
            return False
    if "source" in updates:
        updates["source"] = str(updates["source"]).strip()
        if not updates["source"]:
            return False
    if "frame_interval_seconds" in updates:
        val = updates["frame_interval_seconds"]
        if val is None or val == "":
            updates["frame_interval_seconds"] = None
        else:
            updates["frame_interval_seconds"] = max(0.5, float(val))

    with session_scope() as session:
        row = session.get(Camera, camera_id)
        if row is None:
            return False
        protocol = updates.get("protocol", row.protocol)
        source = updates.get("source", row.source)
        if parse_camera_source(protocol, source) is None:
            return False
        if source_in_use(protocol, source, exclude_id=camera_id):
            return False
        for key, value in updates.items():
            setattr(row, key, value)
        row.updated_at = datetime.datetime.now(datetime.timezone.utc)
        return True


def delete_camera(camera_id: int) -> bool:
    with session_scope() as session:
        row = session.get(Camera, camera_id)
        if row is None:
            return False
        session.delete(row)
        return True


def camera_count() -> int:
    with session_scope() as session:
        return int(session.scalar(select(func.count()).select_from(Camera)) or 0)


def bootstrap_cameras_from_env() -> None:
    """Seed cameras table from env when empty (first install)."""
    if camera_count() > 0:
        return

    entry_url = os.environ.get("CAMERA_URL_ENTRY", "").strip()
    exit_url = os.environ.get("CAMERA_URL_EXIT", "").strip()
    if entry_url or exit_url:
        if entry_url:
            insert_camera(
                name="Entry gate",
                protocol=infer_protocol(entry_url),
                source=entry_url,
                gate_role="entry",
            )
        if exit_url:
            insert_camera(
                name="Exit gate",
                protocol=infer_protocol(exit_url),
                source=exit_url,
                gate_role="exit",
            )
        return

    url = os.environ.get("CAMERA_URL", "0").strip()
    if not url:
        return
    direction = os.environ.get("GATE_DIRECTION", "entry").strip().lower()
    if direction not in VALID_GATE_ROLES:
        direction = "entry"
    insert_camera(
        name="Main gate",
        protocol=infer_protocol(url),
        source=url,
        gate_role=direction,
    )
