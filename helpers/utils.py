import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "./uploads")
COLLECTION_FOLDER = os.environ.get("COLLECTION_FOLDER", "./collection")

# Temp frames for in-flight OCR (deleted after processing).
UPLOAD_TEMP_FOLDER = os.path.join(UPLOAD_FOLDER, "temp")

# Parking event snapshots — split by registered vs unregistered.
UNKNOWN_PARKING_FOLDER = os.path.join(UPLOAD_FOLDER, "unknown_parking_logs")
KNOWN_PARKING_FOLDER = os.path.join(UPLOAD_FOLDER, "known_parking_logs")

PARKING_UNKNOWN_SOURCE_FOLDER = os.path.join(UNKNOWN_PARKING_FOLDER, "sources")
PARKING_UNKNOWN_CROP_FOLDER = os.path.join(UNKNOWN_PARKING_FOLDER, "crops")
PARKING_KNOWN_SOURCE_FOLDER = os.path.join(KNOWN_PARKING_FOLDER, "sources")
PARKING_KNOWN_CROP_FOLDER = os.path.join(KNOWN_PARKING_FOLDER, "crops")

# Backward-compatible aliases (unknown / unregistered).
PARKING_SOURCE_FOLDER = PARKING_UNKNOWN_SOURCE_FOLDER
PARKING_CROP_FOLDER = PARKING_UNKNOWN_CROP_FOLDER


def parking_snapshot_dirs(*, registered: bool) -> tuple[str, str]:
    """Return (source_folder, crop_folder) for a parking log event."""
    if registered:
        return PARKING_KNOWN_SOURCE_FOLDER, PARKING_KNOWN_CROP_FOLDER
    return PARKING_UNKNOWN_SOURCE_FOLDER, PARKING_UNKNOWN_CROP_FOLDER


def ensure_upload_dirs() -> None:
    for path in (
        UPLOAD_FOLDER,
        COLLECTION_FOLDER,
        UPLOAD_TEMP_FOLDER,
        UNKNOWN_PARKING_FOLDER,
        KNOWN_PARKING_FOLDER,
        PARKING_UNKNOWN_SOURCE_FOLDER,
        PARKING_UNKNOWN_CROP_FOLDER,
        PARKING_KNOWN_SOURCE_FOLDER,
        PARKING_KNOWN_CROP_FOLDER,
    ):
        os.makedirs(path, exist_ok=True)


ensure_upload_dirs()


def gate_direction() -> str:
    d = os.environ.get("GATE_DIRECTION", "entry").strip().lower()
    return d if d in ("entry", "exit") else "entry"
