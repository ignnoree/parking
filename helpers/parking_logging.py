"""Parking log persistence + cooldown (no Chroma / embedding)."""

from __future__ import annotations

import collections
import datetime
import json
import logging
import os
import shutil
import threading
import uuid

import cv2

from database.logs_db import log_parking_event
from helpers.plate_crop import crop_frame_region, draw_plate_box_on_crop, expand_plate_box
from helpers.utils import PARKING_CROP_FOLDER, PARKING_SOURCE_FOLDER

PARKING_LOG_COOLDOWN_SECONDS = int(os.environ.get("PARKING_LOG_COOLDOWN_SECONDS", "600"))
# Suppress only OCR jitter on the *same* car (similar reads). Different plates are never blocked.
PARKING_JITTER_COOLDOWN_SECONDS = int(os.environ.get("PARKING_JITTER_COOLDOWN_SECONDS", "20"))
PARKING_READ_STABILITY_COUNT = max(1, int(os.environ.get("PARKING_READ_STABILITY_COUNT", "2")))
PARKING_READ_STABILITY_WINDOW_SECONDS = max(
    1.0, float(os.environ.get("PARKING_READ_STABILITY_WINDOW_SECONDS", "8"))
)

_lock = threading.Lock()
_last_parking_log_at: dict[str, datetime.datetime] = {}
_read_history: collections.deque[tuple[str, datetime.datetime]] = collections.deque(maxlen=80)
_recent_unregistered_logs: collections.deque[tuple[str, str, datetime.datetime]] = collections.deque(
    maxlen=50
)


def _relpath(path: str) -> str:
    return os.path.relpath(path, start=os.getcwd()).replace("\\", "/")


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _plate_digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _plates_similar(a: str, b: str) -> bool:
    """True when two reads are likely the same physical plate with OCR noise."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 2:
        return False
    da, db = _plate_digits(a), _plate_digits(b)
    if len(da) >= 5 and len(db) >= 5 and da[-5:] == db[-5:]:
        return True
    if len(a) >= 6 and len(b) >= 6 and a[:4] == b[:4]:
        return _edit_distance(a, b) <= 3
    return _edit_distance(a, b) <= 2


def _stable_unregistered_read(norm: str, now_utc: datetime.datetime) -> bool:
    """Require repeated similar reads before logging (filters single-frame OCR garbage)."""
    if PARKING_READ_STABILITY_COUNT <= 1:
        return True
    window = datetime.timedelta(seconds=PARKING_READ_STABILITY_WINDOW_SECONDS)
    matches = 0
    for prev_norm, seen_at in reversed(_read_history):
        if now_utc - seen_at > window:
            break
        if _plates_similar(prev_norm, norm):
            matches += 1
            if matches >= PARKING_READ_STABILITY_COUNT:
                return True
    return False


def _jitter_cooldown_active(norm: str, direction: str, now_utc: datetime.datetime) -> bool:
    """Block only if we already logged a *similar* plate recently — not different cars."""
    if PARKING_JITTER_COOLDOWN_SECONDS <= 0:
        return False
    window = datetime.timedelta(seconds=PARKING_JITTER_COOLDOWN_SECONDS)
    for prev_norm, prev_dir, logged_at in reversed(_recent_unregistered_logs):
        if prev_dir != direction:
            continue
        if now_utc - logged_at > window:
            break
        if _plates_similar(prev_norm, norm):
            return True
    return False


def _persist_source_frame(frame_path: str, scan_id: str) -> str | None:
    name = f"scan_{scan_id}_source.jpg"
    dest = os.path.join(PARKING_SOURCE_FOLDER, name)
    try:
        shutil.copy2(frame_path, dest)
        return _relpath(dest)
    except OSError:
        logging.warning("Failed to persist source frame %s", frame_path, exc_info=True)
        return None


def _persist_plate_crop(
    frame_path: str,
    plate_box: dict | None,
    plate_normalized: str,
    direction: str,
    scan_id: str,
) -> str | None:
    frame = cv2.imread(frame_path)
    if frame is None:
        logging.warning("Could not read frame for plate crop: %s", frame_path)
        return None

    h, w = frame.shape[:2]
    expanded = expand_plate_box(plate_box, w, h)
    crop = crop_frame_region(frame, plate_box)
    if crop is None or expanded is None or not plate_box:
        return None

    annotated = draw_plate_box_on_crop(crop, plate_box, expanded)
    safe_plate = "".join(ch if ch.isalnum() else "_" for ch in plate_normalized)[:24] or "plate"
    name = f"crop_{safe_plate}_{direction}_{scan_id}.jpg"
    dest = os.path.join(PARKING_CROP_FOLDER, name)
    if not cv2.imwrite(dest, annotated):
        logging.warning("Failed to write plate crop %s", dest)
        return None
    return _relpath(dest)


def log_parking_events_for_results(frame_path: str, result_payload: dict) -> None:
    if not result_payload or result_payload.get("status") != "ok":
        return

    direction = str(result_payload.get("direction") or "entry")
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    results = [item for item in (result_payload.get("results") or []) if isinstance(item, dict)]
    if not results:
        return

    scan_id = uuid.uuid4().hex[:12]
    source_frame: str | None = None
    logged_any = False

    for item in results:
        norm = item.get("plate_normalized")
        if not norm:
            continue
        match_status = item.get("match_status") or "unregistered"
        vehicle_id = item.get("vehicle_id")

        if match_status == "registered" and vehicle_id is not None:
            key = f"registered:{int(vehicle_id)}:{direction}"
        else:
            key = f"unregistered:{norm}:{direction}"

        with _lock:
            if match_status != "registered":
                if _jitter_cooldown_active(norm, direction, now_utc):
                    continue
                if not _stable_unregistered_read(norm, now_utc):
                    _read_history.append((norm, now_utc))
                    continue
            last = _last_parking_log_at.get(key)
            if last and (now_utc - last).total_seconds() < PARKING_LOG_COOLDOWN_SECONDS:
                _read_history.append((norm, now_utc))
                continue
            _last_parking_log_at[key] = now_utc
            if match_status != "registered":
                _recent_unregistered_logs.append((norm, direction, now_utc))
            _read_history.append((norm, now_utc))

        if source_frame is None:
            source_frame = _persist_source_frame(frame_path, scan_id)

        crop_path = _persist_plate_crop(
            frame_path,
            item.get("box"),
            norm,
            direction,
            f"{scan_id}_{uuid.uuid4().hex[:6]}",
        )

        details = json.dumps(
            {
                "cooldown_s": PARKING_LOG_COOLDOWN_SECONDS,
                "jitter_cooldown_s": PARKING_JITTER_COOLDOWN_SECONDS,
                "source_frame": source_frame,
                "crop_frame": crop_path,
                "plate_box": item.get("box"),
                "plates_in_frame": len(results),
            },
            ensure_ascii=False,
        )

        log_parking_event(
            plate_normalized=norm,
            plate_number=item.get("plate_text"),
            direction=direction,
            match_status=match_status,
            vehicle_id=int(vehicle_id) if vehicle_id is not None else None,
            is_guest=bool(item.get("is_guest")),
            confidence=item.get("confidence"),
            snapshot_path=crop_path or source_frame,
            details=details,
        )
        logged_any = True
        logging.info(
            "[PARKING_LOGGED] plate=%s direction=%s status=%s vehicle_id=%s conf=%s crop=%s source=%s",
            norm,
            direction,
            match_status,
            vehicle_id,
            item.get("confidence"),
            crop_path,
            source_frame,
        )

    if logged_any and len(results) > 1:
        logging.info(
            "Logged %s plate(s) from one frame scan_id=%s source=%s",
            len(results),
            scan_id,
            source_frame,
        )
