"""Persist optional reference images for vehicle enrollment."""

from __future__ import annotations

import os
import uuid

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from helpers.utils import COLLECTION_FOLDER

_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def save_reference_image(plate_normalized: str, file_storage: FileStorage) -> str | None:
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(secure_filename(file_storage.filename))[1].lower()
    if ext not in _ALLOWED_EXT:
        ext = ".jpg"
    safe_plate = "".join(ch if ch.isalnum() else "_" for ch in plate_normalized)[:32] or "plate"
    name = f"{safe_plate}_{uuid.uuid4().hex[:10]}{ext}"
    dest = os.path.join(COLLECTION_FOLDER, name)
    try:
        file_storage.save(dest)
        return os.path.relpath(dest, start=os.getcwd()).replace("\\", "/")
    except OSError:
        return None
