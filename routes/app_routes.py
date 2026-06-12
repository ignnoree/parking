import json
import os
import shutil
from urllib.parse import quote

from flask import Blueprint, Response, abort, jsonify, request, send_file
from flask_jwt_extended import jwt_required

from database.db import reset_db
from database.logs_db import (
    count_parking_logs,
    count_software_logs,
    list_parking_logs,
    list_software_logs,
    log_software_event,
    soft_delete_parking_log,
)
from database.vehicles_db import (
    count_vehicles,
    find_vehicle_by_normalized,
    insert_vehicle,
    list_vehicles,
    soft_delete_vehicle,
)
from helpers.enroll_images import save_reference_image
from database.cameras_db import (
    VALID_GATE_ROLES,
    VALID_LIGHT_PROFILES,
    VALID_PROTOCOLS,
    delete_camera,
    get_camera_by_id,
    insert_camera,
    list_cameras,
    parse_camera_source,
    source_in_use,
    update_camera,
)
from database.settings_db import BOOTSTRAP_KEYS, get_setting, list_settings, set_setting
from database.admin_db import (
    ROLE_SYSTEM_ADMIN,
    ROLE_PARKING_ADMIN,
    ROLE_WORKER,
    VALID_ROLES,
    delete_admin_by_id,
    get_admin_by_id,
    insert_admin,
    list_admins,
    update_admin,
)
from helpers.rbac import get_current_admin, require_admin_roles
from helpers.plate_normalize import normalize_plate
from helpers.live_frame_buffer import get_frame_sequence, get_stream_status, wait_for_new_jpeg
from helpers.utils import UPLOAD_FOLDER, COLLECTION_FOLDER
from workers.camera_worker import get_worker_status, reload_cameras

app_bp = Blueprint("app_routes", __name__)


def _snapshot_path(rel: str | None) -> str | None:
    if not rel:
        return None
    allowed_roots = [
        os.path.abspath(UPLOAD_FOLDER),
        os.path.abspath(COLLECTION_FOLDER),
    ]
    path = os.path.normpath(os.path.join(os.getcwd(), rel.replace("/", os.sep)))
    if not any(path.startswith(root) for root in allowed_roots):
        return None
    return path if os.path.isfile(path) else None


@app_bp.get("/favicon.ico")
def favicon():
    return "", 204


@app_bp.get("/health")
def health():
    try:
        from database.db import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify(
        {
            "status": "ok" if db_ok else "degraded",
            "database": db_ok,
            "camera": get_stream_status(),
            "camera_worker": get_worker_status(),
        }
    ), (200 if db_ok else 503)


@app_bp.get("/")
def index():
    with open("templates/ui.html", encoding="utf-8") as f:
        return f.read()


@app_bp.get("/login")
def login_page():
    with open("templates/login.html", encoding="utf-8") as f:
        return f.read()


@app_bp.get("/submit")
def submit_page():
    with open("templates/submit_ui.html", encoding="utf-8") as f:
        return f.read()


@app_bp.get("/vehicles")
def vehicles_page():
    with open("templates/vehicles.html", encoding="utf-8") as f:
        return f.read()


@app_bp.get("/admin")
def admin_page():
    with open("templates/admin.html", encoding="utf-8") as f:
        return f.read()


@app_bp.get("/api/live/status")
@jwt_required()
def live_status():
    return jsonify({"status": "ok", **get_stream_status(), "camera_worker": get_worker_status()})


@app_bp.get("/api/cameras")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def cameras_list_api():
    return jsonify({"cameras": list_cameras()})


@app_bp.post("/api/cameras")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def cameras_create_api():
    data = request.get_json() or {}
    protocol = str(data.get("protocol") or "rtsp").strip().lower()
    source = str(data.get("source") or "").strip()
    if parse_camera_source(protocol, source) is None:
        return jsonify({"status": "error", "message": "Invalid camera data"}), 400
    if source_in_use(protocol, source):
        return jsonify({"status": "error", "message": "A camera with this source already exists"}), 409
    camera_id = insert_camera(
        name=str(data.get("name") or "").strip(),
        protocol=protocol,
        source=source,
        gate_role=str(data.get("gate_role") or "entry").strip().lower(),
        is_enabled=bool(data.get("is_enabled", True)),
        frame_interval_seconds=data.get("frame_interval_seconds"),
        light_profile=str(data.get("light_profile") or "normal").strip().lower(),
    )
    if camera_id is None:
        return jsonify({"status": "error", "message": "Invalid camera data"}), 400
    reload_cameras()
    row = get_camera_by_id(camera_id)
    log_software_event(
        level="INFO",
        event="camera.created",
        module="app_routes",
        message=f"Camera created id={camera_id}",
        metadata=f"name={row.get('name') if row else camera_id}",
    )
    return jsonify({"status": "ok", "camera": row}), 201


@app_bp.patch("/api/cameras/<int:camera_id>")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def cameras_update_api(camera_id: int):
    data = request.get_json() or {}
    if not data:
        return jsonify({"status": "error", "message": "No fields to update"}), 400
    existing = get_camera_by_id(camera_id)
    if not existing:
        return jsonify({"status": "error", "message": "Camera not found"}), 404
    protocol = str(data.get("protocol", existing["protocol"])).strip().lower()
    source = str(data.get("source", existing["source"])).strip()
    if "protocol" in data or "source" in data:
        if parse_camera_source(protocol, source) is None:
            return jsonify({"status": "error", "message": "Invalid camera data"}), 400
        if source_in_use(protocol, source, exclude_id=camera_id):
            return jsonify({"status": "error", "message": "A camera with this source already exists"}), 409
    ok = update_camera(camera_id, **data)
    if not ok:
        return jsonify({"status": "error", "message": "Camera not found or invalid data"}), 404
    reload_cameras()
    log_software_event(
        level="INFO",
        event="camera.updated",
        module="app_routes",
        message=f"Camera updated id={camera_id}",
    )
    return jsonify({"status": "ok", "camera": get_camera_by_id(camera_id)})


@app_bp.delete("/api/cameras/<int:camera_id>")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def cameras_delete_api(camera_id: int):
    if not delete_camera(camera_id):
        return jsonify({"status": "error", "message": "Camera not found"}), 404
    reload_cameras()
    log_software_event(
        level="INFO",
        event="camera.deleted",
        module="app_routes",
        message=f"Camera deleted id={camera_id}",
    )
    return jsonify({"status": "ok"})


@app_bp.get("/api/settings")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def settings_list_api():
    return jsonify(
        {
            "settings": list_settings(),
            "allowed_keys": list(BOOTSTRAP_KEYS),
            "options": {
                "protocols": list(VALID_PROTOCOLS),
                "gate_roles": list(VALID_GATE_ROLES),
                "light_profiles": list(VALID_LIGHT_PROFILES),
            },
        }
    )


@app_bp.patch("/api/settings/<key>")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def settings_update_api(key: str):
    if key not in BOOTSTRAP_KEYS:
        return jsonify({"status": "error", "message": "Setting key not allowed"}), 400
    data = request.get_json() or {}
    if "value" not in data:
        return jsonify({"status": "error", "message": "value required"}), 400
    payload = data["value"]
    if not isinstance(payload, dict):
        payload = {"value": payload}
    set_setting(key, payload)
    if key == "CAMERA_FRAME_INTERVAL_SECONDS":
        reload_cameras()
    return jsonify({"status": "ok", "setting": {"key": key, "value": get_setting(key)}})


@app_bp.get("/api/software-logs")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def software_logs_api():
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = min(100, max(1, int(request.args.get("page_size", 50) or 50)))
    offset = (page - 1) * page_size
    filters = {
        "level": request.args.get("level"),
        "event": request.args.get("event"),
        "module": request.args.get("module"),
    }
    active = {k: v for k, v in filters.items() if v}
    total = count_software_logs(**active)
    logs = list_software_logs(limit=page_size, offset=offset, **active)
    return jsonify(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": offset + page_size < total,
            "has_prev": page > 1,
            "logs": logs,
        }
    )


@app_bp.get("/api/admins")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN)
def admins_list_api():
    return jsonify({"admins": list_admins()})


@app_bp.post("/api/admins")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN)
def admins_create_api():
    data = request.get_json() or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    role = str(data.get("role") or ROLE_WORKER).strip().lower()
    if not username or not password:
        return jsonify({"status": "error", "message": "username and password required"}), 400
    if role not in VALID_ROLES:
        return jsonify({"status": "error", "message": f"role must be one of {VALID_ROLES}"}), 400
    admin_id = insert_admin(username, password, role)
    if admin_id is None:
        return jsonify({"status": "error", "message": "Username already exists or invalid role"}), 409
    row = get_admin_by_id(admin_id)
    if row:
        row.pop("password_hash", None)
        row.pop("refresh_jti", None)
    log_software_event(
        level="INFO",
        event="admin.created",
        module="app_routes",
        message=f"Admin account created id={admin_id}",
        metadata=f"username={username!r} role={role!r}",
    )
    return jsonify({"status": "ok", "admin": row}), 201


@app_bp.patch("/api/admins/<int:admin_id>")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN)
def admins_update_api(admin_id: int):
    data = request.get_json() or {}
    if not data:
        return jsonify({"status": "error", "message": "No fields to update"}), 400
    existing = get_admin_by_id(admin_id)
    if not existing:
        return jsonify({"status": "error", "message": "Admin not found"}), 404
    role = data.get("role")
    if role is not None:
        role = str(role).strip().lower()
    password = data.get("password")
    if password is not None:
        password = str(password)
        if not password:
            return jsonify({"status": "error", "message": "password cannot be empty"}), 400
    if not update_admin(admin_id, role=role, password_plain=password):
        return jsonify({"status": "error", "message": "Invalid update"}), 400
    row = get_admin_by_id(admin_id)
    if row:
        row.pop("password_hash", None)
        row.pop("refresh_jti", None)
    log_software_event(
        level="INFO",
        event="admin.updated",
        module="app_routes",
        message=f"Admin account updated id={admin_id}",
    )
    return jsonify({"status": "ok", "admin": row})


@app_bp.delete("/api/admins/<int:admin_id>")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN)
def admins_delete_api(admin_id: int):
    current = get_current_admin()
    if current and int(current["id"]) == admin_id:
        return jsonify({"status": "error", "message": "Cannot delete your own account"}), 400
    existing = get_admin_by_id(admin_id)
    if not existing:
        return jsonify({"status": "error", "message": "Admin not found"}), 404
    if existing.get("role") == ROLE_SYSTEM_ADMIN:
        others = [a for a in list_admins() if a.get("role") == ROLE_SYSTEM_ADMIN and a["id"] != admin_id]
        if not others:
            return jsonify({"status": "error", "message": "Cannot delete the last system_admin"}), 400
    if not delete_admin_by_id(admin_id):
        return jsonify({"status": "error", "message": "Admin not found"}), 404
    log_software_event(
        level="WARN",
        event="admin.deleted",
        module="app_routes",
        message=f"Admin account deleted id={admin_id}",
        metadata=f"username={existing.get('username')!r}",
    )
    return jsonify({"status": "ok"})


@app_bp.get("/api/live/stream")
@jwt_required()
def live_stream():
    worker = get_worker_status()
    if worker.get("camera_count", 0) == 0:
        return jsonify({"status": "error", "message": "No camera configured"}), 503

    fps = min(60.0, max(1.0, float(os.environ.get("LIVE_STREAM_MAX_FPS", "15"))))
    pause = 1.0 / fps
    boundary = "frame"

    def generate():
        seq = get_frame_sequence()
        while True:
            jpeg, seq = wait_for_new_jpeg(seq, timeout=pause)
            if jpeg:
                yield b"--" + boundary.encode() + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

    return Response(
        generate(),
        mimetype=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app_bp.get("/api/parking-logs")
@jwt_required()
def parking_logs_api():
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = min(50, max(1, int(request.args.get("page_size", 10) or 10)))
    offset = (page - 1) * page_size
    filters = {
        "direction": request.args.get("direction"),
        "match_status": request.args.get("match_status"),
        "plate": request.args.get("plate"),
        "from_date": request.args.get("from_date") or request.args.get("from"),
        "to_date": request.args.get("to_date") or request.args.get("to"),
    }
    active = {k: v for k, v in filters.items() if v}
    include_deleted = request.args.get("include_deleted", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if include_deleted:
        admin = get_current_admin()
        if not admin or admin.get("role") != ROLE_SYSTEM_ADMIN:
            return jsonify({"status": "error", "message": "include_deleted requires system_admin"}), 403
    total = count_parking_logs(include_deleted_vehicles=include_deleted, **active)
    logs = list_parking_logs(
        limit=page_size,
        offset=offset,
        include_deleted_vehicles=include_deleted,
        **active,
    )
    for row in logs:
        snap = row.get("snapshot_path")
        row["snapshot_url"] = f"/api/parking-snapshot?path={quote(str(snap))}" if snap else None
        source_path = None
        raw_details = row.get("details")
        if raw_details:
            try:
                meta = json.loads(raw_details)
                if isinstance(meta, dict):
                    source_path = meta.get("source_frame")
            except json.JSONDecodeError:
                pass
        row["source_snapshot_url"] = (
            f"/api/parking-snapshot?path={quote(str(source_path))}" if source_path else None
        )
    return jsonify(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": offset + page_size < total,
            "has_prev": page > 1,
            "logs": logs,
        }
    )


@app_bp.delete("/api/parking-logs/<int:log_id>")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN)
def parking_log_soft_delete_api(log_id: int):
    if not soft_delete_parking_log(log_id):
        return jsonify({"status": "error", "message": "Parking log not found"}), 404
    log_software_event(
        level="INFO",
        event="parking_log.soft_deleted",
        module="app_routes",
        message=f"Parking log soft-deleted id={log_id}",
    )
    return jsonify({"status": "ok"})


@app_bp.get("/api/parking-snapshot")
@jwt_required()
def parking_snapshot():
    path = _snapshot_path(request.args.get("path"))
    if not path:
        abort(404)
    return send_file(path)


@app_bp.get("/api/vehicles")
@jwt_required()
def vehicles_api():
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = min(100, max(1, int(request.args.get("page_size", 50) or 50)))
    offset = (page - 1) * page_size
    is_guest_raw = request.args.get("is_guest")
    is_guest = None
    if is_guest_raw is not None and str(is_guest_raw).strip() != "":
        is_guest = str(is_guest_raw).strip().lower() in {"1", "true", "yes", "on"}
    filters = {
        "plate": request.args.get("plate"),
        "owner": request.args.get("owner"),
        "is_guest": is_guest,
    }
    active = {k: v for k, v in filters.items() if v is not None and v != ""}
    total = count_vehicles(**active)
    vehicles = list_vehicles(limit=page_size, offset=offset, **active)
    for row in vehicles:
        ref = row.get("reference_image_path")
        row["reference_image_url"] = (
            f"/api/parking-snapshot?path={quote(str(ref))}" if ref else None
        )
    return jsonify(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": offset + page_size < total,
            "has_prev": page > 1,
            "vehicles": vehicles,
        }
    )


def _parse_enroll_payload():
    import datetime

    if request.content_type and "multipart/form-data" in request.content_type:
        form = request.form
        data = {k: form.get(k) for k in form.keys()}
        is_guest = str(data.get("is_guest", "")).strip().lower() in {"1", "true", "yes", "on"}
        ref_file = request.files.get("reference_image")
    else:
        data = request.get_json(silent=True) or {}
        is_guest = bool(data.get("is_guest"))
        ref_file = None
    plate = data.get("plate_number")
    if not plate:
        return None, None, (jsonify({"status": "error", "message": "plate_number required"}), 400)
    guest_expires_at = data.get("guest_expires_at")
    if is_guest and not guest_expires_at:
        return None, None, (
            jsonify({"status": "error", "message": "guest_expires_at required for guest"}),
            400,
        )
    exp_dt = None
    if guest_expires_at:
        exp_dt = datetime.datetime.fromisoformat(str(guest_expires_at).replace("Z", "+00:00"))
    meta = data.get("metadata")
    if isinstance(meta, str) and meta.strip():
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    payload = {
        "plate_number": str(plate).strip(),
        "owner_name": data.get("owner_name") or None,
        "owner_lastname": data.get("owner_lastname") or None,
        "car_model": data.get("car_model") or None,
        "door_number": data.get("door_number") or None,
        "floor_number": data.get("floor_number") or None,
        "parking_spot": data.get("parking_spot") or None,
        "plate_color": data.get("plate_color") or "default",
        "vehicle_class": data.get("vehicle_class") or "car",
        "is_guest": is_guest,
        "guest_expires_at": exp_dt,
        "metadata": meta if isinstance(meta, dict) else None,
        "reference_file": ref_file,
    }
    return payload, normalize_plate(str(plate)), None


@app_bp.post("/api/enroll")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN, ROLE_WORKER)
def enroll_vehicle():
    payload, norm, err = _parse_enroll_payload()
    if err:
        return err
    existing = find_vehicle_by_normalized(norm)
    if existing:
        return jsonify(
            {
                "status": "ok",
                "duplicate": True,
                "vehicle_id": existing["id"],
                "plate_number_normalized": norm,
            }
        )
    ref_path = None
    ref_file = payload.pop("reference_file", None)
    if ref_file:
        ref_path = save_reference_image(norm, ref_file)
    created = insert_vehicle(reference_image_path=ref_path, **payload)
    if not created:
        return jsonify({"status": "error", "message": "Could not enroll vehicle"}), 400
    vid, normalized = created
    log_software_event(
        level="INFO",
        event="vehicle.enrolled",
        module="app_routes",
        message=f"Vehicle enrolled id={vid}",
        metadata=f"plate={normalized!r} guest={payload['is_guest']}",
    )
    return jsonify(
        {
            "status": "ok",
            "duplicate": False,
            "vehicle_id": vid,
            "plate_number_normalized": normalized,
            "reference_image_path": ref_path,
        }
    )


@app_bp.post("/api/remove-vehicle")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN, ROLE_WORKER)
def remove_vehicle():
    data = request.get_json() or {}
    ok = soft_delete_vehicle(vehicle_id=data.get("vehicle_id"), plate_number=data.get("plate_number"))
    if not ok:
        return jsonify({"status": "error", "message": "Vehicle not found"}), 404
    log_software_event(
        level="INFO",
        event="vehicle.soft_deleted",
        module="app_routes",
        message="Vehicle soft-deleted",
        metadata=str(data),
    )
    return jsonify({"status": "ok"})


@app_bp.get("/reset")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN)
def reset_system():
    reset_db()
    for folder in (UPLOAD_FOLDER, COLLECTION_FOLDER):
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                p = os.path.join(folder, name)
                if os.path.isfile(p):
                    os.remove(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
    log_software_event(level="WARN", event="system.reset", module="app_routes", message="System reset")
    reload_cameras()
    return jsonify({"status": "ok"})
