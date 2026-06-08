import json
import os
import shutil
from urllib.parse import quote

from flask import Blueprint, Response, abort, jsonify, request, send_file
from flask_jwt_extended import jwt_required

from database.db import reset_db
from database.logs_db import count_parking_logs, list_parking_logs, log_software_event
from database.vehicles_db import list_vehicles, soft_delete_vehicle, find_vehicle_by_normalized, insert_vehicle
from database.admin_db import ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN, ROLE_WORKER
from helpers.rbac import require_admin_roles
from helpers.plate_normalize import normalize_plate
from helpers.live_frame_buffer import get_frame_sequence, get_stream_status, wait_for_new_jpeg
from helpers.utils import UPLOAD_FOLDER, COLLECTION_FOLDER

app_bp = Blueprint("app_routes", __name__)


def _snapshot_path(rel: str | None) -> str | None:
    if not rel:
        return None
    root = os.path.abspath(UPLOAD_FOLDER)
    path = os.path.normpath(os.path.join(os.getcwd(), rel.replace("/", os.sep)))
    if not path.startswith(root):
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
    return jsonify({"status": "ok" if db_ok else "degraded", "database": db_ok, "camera": get_stream_status()}), (
        200 if db_ok else 503
    )


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


@app_bp.get("/api/live/status")
@jwt_required()
def live_status():
    return jsonify({"status": "ok", **get_stream_status()})


@app_bp.get("/api/live/stream")
@jwt_required()
def live_stream():
    fps = min(60.0, max(1.0, float(os.environ.get("LIVE_STREAM_MAX_FPS", "15"))))
    pause = 1.0 / fps
    boundary = "frame"

    def generate():
        seq = get_frame_sequence()
        while True:
            jpeg, seq = wait_for_new_jpeg(seq, timeout=pause)
            if jpeg:
                yield b"--" + boundary.encode() + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

    return Response(generate(), mimetype=f"multipart/x-mixed-replace; boundary={boundary}")


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
    }
    total = count_parking_logs(**{k: v for k, v in filters.items() if v})
    logs = list_parking_logs(limit=page_size, offset=offset, **{k: v for k, v in filters.items() if v})
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
    return jsonify(
        {
            "vehicles": list_vehicles(
                plate=request.args.get("plate"),
                limit=min(200, int(request.args.get("limit", 100) or 100)),
            )
        }
    )


@app_bp.post("/api/enroll")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN, ROLE_WORKER)
def enroll_vehicle():
    data = request.get_json() or {}
    plate = data.get("plate_number")
    if not plate:
        return jsonify({"status": "error", "message": "plate_number required"}), 400
    norm = normalize_plate(plate)
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
    is_guest = bool(data.get("is_guest"))
    guest_expires_at = data.get("guest_expires_at")
    if is_guest and not guest_expires_at:
        return jsonify({"status": "error", "message": "guest_expires_at required for guest"}), 400
    import datetime

    exp_dt = None
    if guest_expires_at:
        exp_dt = datetime.datetime.fromisoformat(str(guest_expires_at).replace("Z", "+00:00"))
    created = insert_vehicle(
        plate_number=plate,
        owner_name=data.get("owner_name"),
        owner_lastname=data.get("owner_lastname"),
        car_model=data.get("car_model"),
        door_number=data.get("door_number"),
        floor_number=data.get("floor_number"),
        parking_spot=data.get("parking_spot"),
        plate_color=data.get("plate_color") or "default",
        vehicle_class=data.get("vehicle_class") or "car",
        is_guest=is_guest,
        guest_expires_at=exp_dt,
        metadata=data.get("metadata"),
    )
    if not created:
        return jsonify({"status": "error", "message": "Could not enroll vehicle"}), 400
    vid, normalized = created
    return jsonify(
        {"status": "ok", "duplicate": False, "vehicle_id": vid, "plate_number_normalized": normalized}
    )


@app_bp.post("/api/remove-vehicle")
@jwt_required()
@require_admin_roles(ROLE_SYSTEM_ADMIN, ROLE_PARKING_ADMIN, ROLE_WORKER)
def remove_vehicle():
    data = request.get_json() or {}
    ok = soft_delete_vehicle(vehicle_id=data.get("vehicle_id"), plate_number=data.get("plate_number"))
    if not ok:
        return jsonify({"status": "error", "message": "Vehicle not found"}), 404
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
    return jsonify({"status": "ok"})
