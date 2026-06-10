import atexit
import datetime
import logging
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

from flask import Flask, jsonify
from flask_jwt_extended import JWTManager
from werkzeug.exceptions import HTTPException

from database.db import bootstrap_db
from routes.auth_routes import auth_bp
from routes.app_routes import app_bp
from helpers.utils import UPLOAD_FOLDER
from helpers.guest_expiry import purge_expired_guest_vehicles, start_guest_expiry_thread
from helpers.plate_detect_isolated import shutdown as shutdown_plate_worker
from workers.camera_worker import start_camera_worker_thread


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "change-this-secret")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = datetime.timedelta(minutes=15)
app.config["JWT_REFRESH_TOKEN_EXPIRES"] = datetime.timedelta(days=7)
app.config["JWT_TOKEN_LOCATION"] = ["headers", "cookies"]
app.config["JWT_COOKIE_SECURE"] = _env_bool("JWT_COOKIE_SECURE")
app.config["JWT_COOKIE_CSRF_PROTECT"] = False

jwt = JWTManager(app)


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    if exc.code and exc.code >= 500:
        logging.exception("HTTP error: %s", exc)
        from database.logs_db import log_software_event

        log_software_event(
            level="ERROR",
            event="app.http_exception",
            module="main",
            message=str(exc),
        )
    return jsonify({"error": exc.name, "message": exc.description}), exc.code


@app.errorhandler(Exception)
def handle_unhandled(exc):
    if isinstance(exc, HTTPException):
        return handle_http_exception(exc)
    logging.exception("Unhandled error: %s", exc)
    from database.logs_db import log_software_event

    log_software_event(
        level="ERROR",
        event="app.unhandled_exception",
        module="main",
        message=str(exc),
    )
    return jsonify({"error": "Server error"}), 500


app.register_blueprint(auth_bp)
app.register_blueprint(app_bp)

bootstrap_db()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
atexit.register(shutdown_plate_worker)


def start_background_workers() -> None:
    purge_expired_guest_vehicles()
    start_guest_expiry_thread()
    start_camera_worker_thread()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    start_background_workers()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=_env_bool("FLASK_DEBUG"))
