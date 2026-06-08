import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "./uploads")
COLLECTION_FOLDER = os.environ.get("COLLECTION_FOLDER", "./collection")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(COLLECTION_FOLDER, exist_ok=True)

UNKNOWN_PARKING_FOLDER = os.path.join(UPLOAD_FOLDER, "unknown_parking_logs")
PARKING_SOURCE_FOLDER = os.path.join(UNKNOWN_PARKING_FOLDER, "sources")
PARKING_CROP_FOLDER = os.path.join(UNKNOWN_PARKING_FOLDER, "crops")
os.makedirs(UNKNOWN_PARKING_FOLDER, exist_ok=True)
os.makedirs(PARKING_SOURCE_FOLDER, exist_ok=True)
os.makedirs(PARKING_CROP_FOLDER, exist_ok=True)


def gate_direction() -> str:
    d = os.environ.get("GATE_DIRECTION", "entry").strip().lower()
    return d if d in ("entry", "exit") else "entry"
