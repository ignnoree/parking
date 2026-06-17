from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
    set_access_cookies,
    set_refresh_cookies,
)
from flask_jwt_extended.utils import decode_token

from database.admin_db import get_admin_by_username, get_admin_by_id, update_admin_refresh_jti, ROLE_WORKER
from database.logs_db import log_software_event

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _token_claims_for_admin(admin: dict) -> dict:
    return {"role": admin.get("role") or ROLE_WORKER}


@auth_bp.post("/login")
def login():
    data = request.get_json() or {}
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    admin = get_admin_by_username(username)
    if not admin or not check_password_hash(admin["password_hash"], password):
        log_software_event(
            level="WARN",
            event="auth.login.failed",
            module="routes.auth_routes",
            message="Invalid login credentials",
            metadata=f"username={username!r}",
            admin_id=admin["id"] if admin else None,
            admin_username=str(username),
        )
        return jsonify({"error": "Invalid credentials"}), 401
    admin_id = admin["id"]
    extra = _token_claims_for_admin(admin)
    access_token = create_access_token(identity=str(admin_id), additional_claims=extra)
    refresh_token = create_refresh_token(identity=str(admin_id), additional_claims=extra)
    refresh_jti = decode_token(refresh_token)["jti"]
    update_admin_refresh_jti(admin_id, refresh_jti)
    response = jsonify(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "username": admin["username"],
            "role": extra["role"],
        }
    )
    set_access_cookies(response, access_token)
    set_refresh_cookies(response, refresh_token)
    log_software_event(
        level="INFO",
        event="auth.login.success",
        module="routes.auth_routes",
        message="User logged in",
        metadata=f"admin_id={admin_id} username={admin['username']!r} role={extra['role']!r}",
        admin_id=admin_id,
        admin_username=admin["username"],
    )
    return response


@auth_bp.get("/me")
@jwt_required()
def auth_me():
    admin = get_admin_by_id(get_jwt_identity())
    if not admin:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"id": admin["id"], "username": admin["username"], "role": admin.get("role")})


@auth_bp.post("/refresh")
@jwt_required(refresh=True)
def refresh():
    jwt_data = get_jwt()
    admin_id_raw = get_jwt_identity()
    admin = get_admin_by_id(admin_id_raw) if admin_id_raw else None
    if not admin or admin.get("refresh_jti") != jwt_data.get("jti"):
        log_software_event(
            level="WARN",
            event="auth.refresh.failed",
            module="routes.auth_routes",
            message="Invalid refresh token",
            metadata=f"admin_id={admin_id_raw!r}",
            admin_id=admin_id_raw,
            admin_username=admin["username"] if admin else None,
        )
        return jsonify({"error": "Invalid refresh token"}), 401
    extra = _token_claims_for_admin(admin)
    access_token = create_access_token(identity=str(admin_id_raw), additional_claims=extra)
    refresh_token = create_refresh_token(identity=str(admin_id_raw), additional_claims=extra)
    update_admin_refresh_jti(admin_id_raw, decode_token(refresh_token)["jti"])
    response = jsonify({"access_token": access_token, "refresh_token": refresh_token, "token_type": "Bearer"})
    set_access_cookies(response, access_token)
    set_refresh_cookies(response, refresh_token)
    return response
