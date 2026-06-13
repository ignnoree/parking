"""Parking log persistence + cooldown (no Chroma / embedding)."""

from __future__ import annotations

import collections
import datetime
import json
import logging
import os
import shutil
import threading
import time
import uuid

import cv2

from database.logs_db import log_parking_event
from helpers.plate_cluster import canonical_plate, plates_similar
from helpers.plate_crop import crop_frame_region, draw_plate_box_on_crop, expand_plate_box
from helpers.utils import parking_snapshot_dirs

from helpers.runtime_settings import parking_log_cooldown_seconds

# Suppress only OCR jitter on the *same* car (similar reads). Different plates are never blocked.
PARKING_JITTER_COOLDOWN_SECONDS = int(os.environ.get("PARKING_JITTER_COOLDOWN_SECONDS", "120"))
PARKING_READ_STABILITY_COUNT = max(1, int(os.environ.get("PARKING_READ_STABILITY_COUNT", "2")))
PARKING_READ_STABILITY_WINDOW_SECONDS = max(
    1.0, float(os.environ.get("PARKING_READ_STABILITY_WINDOW_SECONDS", "20"))
)
PARKING_COOLDOWN_MAP_TTL_SECONDS = max(
    0, int(os.environ.get("PARKING_COOLDOWN_MAP_TTL_SECONDS", "86400"))
)

_lock = threading.Lock()
_last_parking_log_at: dict[str, datetime.datetime] = {}
_read_history: collections.deque[tuple[str, datetime.datetime]] = collections.deque(maxlen=120)
_recent_unregistered_logs: collections.deque[tuple[str, str, datetime.datetime]] = collections.deque(
    maxlen=80
)
_recent_uncertain_logs: collections.deque[tuple[str, str, datetime.datetime]] = collections.deque(
    maxlen=80
)


def parking_log_uncertain_enabled() -> bool:
    explicit = os.environ.get("PARKING_LOG_UNCERTAIN_ENABLED", "").strip().lower()
    if explicit:
        return explicit in {"1", "true", "yes", "on"}
    return os.environ.get("PARKING_LOG_SKIPPED_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _relpath(path: str) -> str:
    return os.path.relpath(path, start=os.getcwd()).replace("\\", "/")


def _prune_stale_cooldown_keys(now_utc: datetime.datetime) -> None:
    if PARKING_COOLDOWN_MAP_TTL_SECONDS <= 0:
        return
    ttl = datetime.timedelta(seconds=PARKING_COOLDOWN_MAP_TTL_SECONDS)
    stale = [key for key, seen_at in _last_parking_log_at.items() if now_utc - seen_at > ttl]
    for key in stale:
        _last_parking_log_at.pop(key, None)


# Backward-compatible test hooks.
def _plates_similar(a: str, b: str) -> bool:
    return plates_similar(a, b)


def _stable_unregistered_read(norm: str, now_utc: datetime.datetime) -> bool:
    """Require repeated similar reads before logging (filters single-frame OCR garbage)."""
    if PARKING_READ_STABILITY_COUNT <= 1:
        return True
    window = datetime.timedelta(seconds=PARKING_READ_STABILITY_WINDOW_SECONDS)
    matches = 0
    for prev_norm, seen_at in reversed(_read_history):
        if now_utc - seen_at > window:
            break
        if plates_similar(prev_norm, norm):
            matches += 1
            # Count includes current read: COUNT=2 → log on 2nd agreeing OCR.
            if matches + 1 >= PARKING_READ_STABILITY_COUNT:
                return True
    return False


def _jitter_cooldown_active(canonical: str, direction: str, now_utc: datetime.datetime) -> bool:
    """Block only if we already logged a *similar* plate recently — not different cars."""
    if PARKING_JITTER_COOLDOWN_SECONDS <= 0:
        return False
    window = datetime.timedelta(seconds=PARKING_JITTER_COOLDOWN_SECONDS)
    for prev_norm, prev_dir, logged_at in reversed(_recent_unregistered_logs):
        if prev_dir != direction:
            continue
        if now_utc - logged_at > window:
            break
        if plates_similar(prev_norm, canonical):
            return True
    return False


def _uncertain_jitter_cooldown_active(canonical: str, direction: str, now_utc: datetime.datetime) -> bool:
    if PARKING_JITTER_COOLDOWN_SECONDS <= 0:
        return False
    window = datetime.timedelta(seconds=PARKING_JITTER_COOLDOWN_SECONDS)
    for prev_norm, prev_dir, logged_at in reversed(_recent_uncertain_logs):
        if prev_dir != direction:
            continue
        if now_utc - logged_at > window:
            break
        if plates_similar(prev_norm, canonical):
            return True
    return False


def _uncertain_log_cooldown_active(canonical: str, direction: str, now_utc: datetime.datetime) -> bool:
    """Suppress uncertain when the same car was recently logged (confirmed or uncertain)."""
    if _uncertain_jitter_cooldown_active(canonical, direction, now_utc):
        return True
    if _jitter_cooldown_active(canonical, direction, now_utc):
        return True
    key = f"unregistered:{canonical}:{direction}"
    last = _last_parking_log_at.get(key)
    if last and (now_utc - last).total_seconds() < parking_log_cooldown_seconds():
        return True
    return False


def _persist_source_frame(frame_path: str, scan_id: str, *, source_folder: str) -> str | None:
    name = f"scan_{scan_id}_source.jpg"
    dest = os.path.join(source_folder, name)
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
    *,
    crop_folder: str,
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
    dest = os.path.join(crop_folder, name)
    if not cv2.imwrite(dest, annotated):
        logging.warning("Failed to write plate crop %s", dest)
        return None
    return _relpath(dest)


def _format_wrap_suffix(timing: dict) -> str:
    wrap_s = timing.get("wrap_s")
    if wrap_s is None:
        wrap_s = timing.get("detect_to_log_s")
    if wrap_s is None:
        return ""
    parts = [f" wrap_s={wrap_s}s"]
    ocr_s = timing.get("ocr_elapsed_s")
    if ocr_s is not None:
        parts.append(f" ocr_s={ocr_s}s")
    hits = timing.get("track_hits")
    if hits is not None:
        parts.append(f" hits={hits}")
    return "".join(parts)


def _resolve_item_timing(item: dict, *, wrap_started_at: float | None) -> dict:
    timing = dict(item.get("timing") or {})
    if wrap_started_at is not None and "wrap_s" not in timing and "detect_to_log_s" not in timing:
        timing["wrap_s"] = round(max(0.0, time.monotonic() - wrap_started_at), 3)
    if "wrap_s" not in timing and timing.get("detect_to_log_s") is not None:
        timing["wrap_s"] = timing["detect_to_log_s"]
    return timing


def log_parking_events_for_results(
    frame_path: str,
    result_payload: dict,
    *,
    wrap_started_at: float | None = None,
) -> list[dict]:
    if not result_payload or result_payload.get("status") != "ok":
        return []

    direction = str(result_payload.get("direction") or "entry")
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    results = [item for item in (result_payload.get("results") or []) if isinstance(item, dict)]
    if not results:
        return []

    scan_id = uuid.uuid4().hex[:12]
    source_frames: dict[bool, str | None] = {}
    logged_any = False
    logged_plates: list[dict] = []

    for item in results:
        norm = item.get("plate_normalized")
        if not norm:
            continue
        match_status = item.get("match_status") or "unregistered"
        vehicle_id = item.get("vehicle_id")
        track_confirmed = bool(item.get("track_confirmed"))

        if (
            match_status != "registered"
            and not track_confirmed
            and not _stable_unregistered_read(norm, now_utc)
        ):
            _read_history.append((norm, now_utc))
            continue

        canonical = canonical_plate(norm, now_utc)

        if match_status == "registered" and vehicle_id is not None:
            key = f"registered:{int(vehicle_id)}:{direction}"
        else:
            key = f"unregistered:{canonical}:{direction}"

        with _lock:
            _prune_stale_cooldown_keys(now_utc)
            if match_status != "registered":
                if _jitter_cooldown_active(canonical, direction, now_utc):
                    continue
                if _uncertain_jitter_cooldown_active(canonical, direction, now_utc):
                    continue
            last = _last_parking_log_at.get(key)
            if last and (now_utc - last).total_seconds() < parking_log_cooldown_seconds():
                _read_history.append((canonical, now_utc))
                continue
            _last_parking_log_at[key] = now_utc
            if match_status != "registered":
                _recent_unregistered_logs.append((canonical, direction, now_utc))
            _read_history.append((canonical, now_utc))

        is_registered = match_status == "registered" and vehicle_id is not None
        source_folder, crop_folder = parking_snapshot_dirs(registered=is_registered)
        if is_registered not in source_frames:
            source_frames[is_registered] = _persist_source_frame(
                frame_path, scan_id, source_folder=source_folder
            )
        source_frame = source_frames[is_registered]

        crop_path = _persist_plate_crop(
            frame_path,
            item.get("box"),
            norm,
            direction,
            f"{scan_id}_{uuid.uuid4().hex[:6]}",
            crop_folder=crop_folder,
        )

        timing = _resolve_item_timing(item, wrap_started_at=wrap_started_at)

        details = json.dumps(
            {
                "cooldown_s": parking_log_cooldown_seconds(),
                "jitter_cooldown_s": PARKING_JITTER_COOLDOWN_SECONDS,
                "canonical_plate": canonical,
                "source_frame": source_frame,
                "crop_frame": crop_path,
                "plate_box": item.get("box"),
                "plates_in_frame": len(results),
                "timing": timing,
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
        logged_plates.append(
            {
                "plate_text": item.get("plate_text") or norm,
                "plate_normalized": norm,
                "box": item.get("box"),
                "match_status": match_status,
                "is_guest": bool(item.get("is_guest")),
            }
        )
        logging.info(
            "[PARKING_LOGGED] plate=%s canonical=%s direction=%s status=%s vehicle_id=%s conf=%s%s",
            norm,
            canonical,
            direction,
            match_status,
            vehicle_id,
            item.get("confidence"),
            _format_wrap_suffix(timing),
        )

    if logged_any and len(results) > 1:
        logging.info(
            "Logged %s plate(s) from one frame scan_id=%s source=%s",
            len(results),
            scan_id,
            source_frame,
        )
    return logged_plates


def log_uncertain_track_event(
    frame_path: str,
    *,
    direction: str,
    plate_normalized: str,
    plate_text: str | None = None,
    confidence: float | None = None,
    box: dict | None = None,
    timing: dict | None = None,
    skip_reason: str | None = None,
    track_id: int | None = None,
) -> bool:
    """
    Persist a weak-but-readable plate as match_status=uncertain (audit only).
    Does not bypass jitter cooldown for confirmed logs on the same plate later.
    """
    if not parking_log_uncertain_enabled():
        return False

    norm = str(plate_normalized or "").strip()
    if not norm:
        return False

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    canonical = canonical_plate(norm, now_utc)
    with _lock:
        _prune_stale_cooldown_keys(now_utc)
        if _uncertain_log_cooldown_active(canonical, direction, now_utc):
            logging.debug(
                "[PARKING_UNCERTAIN] suppressed plate=%s direction=%s reason=log_cooldown",
                norm,
                direction,
            )
            return False

    scan_id = uuid.uuid4().hex[:12]
    source_folder, crop_folder = parking_snapshot_dirs(registered=False)
    source_frame = _persist_source_frame(frame_path, scan_id, source_folder=source_folder)
    crop_path = _persist_plate_crop(
        frame_path,
        box,
        norm,
        direction,
        f"{scan_id}_{uuid.uuid4().hex[:6]}",
        crop_folder=crop_folder,
    )

    timing_payload = dict(timing or {})
    if track_id is not None and "track_id" not in timing_payload:
        timing_payload["track_id"] = track_id
    if skip_reason:
        timing_payload["skip_reason"] = skip_reason

    details = json.dumps(
        {
            "canonical_plate": canonical,
            "source_frame": source_frame,
            "crop_frame": crop_path,
            "plate_box": box,
            "timing": timing_payload,
            "skip_reason": skip_reason,
        },
        ensure_ascii=False,
    )

    log_parking_event(
        plate_normalized=norm,
        plate_number=plate_text or norm,
        direction=direction,
        match_status="uncertain",
        vehicle_id=None,
        is_guest=False,
        confidence=confidence,
        snapshot_path=crop_path or source_frame,
        details=details,
    )
    with _lock:
        _recent_uncertain_logs.append((canonical, direction, now_utc))
    logging.info(
        "[PARKING_UNCERTAIN] plate=%s canonical=%s direction=%s conf=%s reason=%s%s",
        norm,
        canonical,
        direction,
        confidence,
        skip_reason or "uncertain",
        _format_wrap_suffix(timing_payload),
    )
    return True
