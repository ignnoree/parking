from functools import wraps

from flask import jsonify
from flask_jwt_extended import get_jwt_identity

from database.admin_db import ROLE_WORKER, get_admin_by_id
from helpers.uuid_utils import parse_uuid


def get_current_admin() -> dict | None:
    raw = get_jwt_identity()
    if raw is None:
        return None
    return get_admin_by_id(raw)


def require_admin_roles(*allowed_roles: str):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            admin = get_current_admin()
            if not admin:
                return jsonify({"error": "Unauthorized"}), 401
            role = admin.get("role") or ROLE_WORKER
            if role not in allowed_roles:
                return jsonify({"error": "Forbidden"}), 403
            return fn(*args, **kwargs)

        return wrapped

    return decorator
