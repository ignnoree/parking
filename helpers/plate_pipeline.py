"""Plate detect + OCR pipeline using YOLO detection and OCR."""

from __future__ import annotations

import datetime
import logging
import os

import cv2

from database.vehicles_db import find_vehicle_by_normalized
from helpers.plate_alpr import alpr_init_error, get_alpr, ocr_confidence_value
from helpers.plate_color import detect_plate_color_from_frame
from helpers.plate_format import clean_plate_ocr_text, is_plausible_plate, plate_format_score
from helpers.plate_normalize import normalize_plate

logger = logging.getLogger(__name__)

# Combined detect×OCR×format gate. Now overridable via PLATE_OCR_MIN_CONFIDENCE env.
# Default 0.42: lower than the old 0.48 so plates with conf ≈ 0.43-0.47 (perfect format,
# strong OCR, weaker YOLO box) still reach the tracker for voting instead of being dropped here.
def plate_ocr_min_confidence() -> float:
    return max(0.0, min(1.0, float(os.environ.get("PLATE_OCR_MIN_CONFIDENCE", "0.42"))))


PLATE_OCR_MIN_CONFIDENCE = plate_ocr_min_confidence()


def plate_format_min_score() -> float:
    return max(0.0, min(1.0, float(os.environ.get("PLATE_FORMAT_MIN_SCORE", "0.55"))))


def plate_debug_logging() -> bool:
    return os.environ.get("PLATE_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _combined_confidence(det_conf: float, ocr_conf: float, fmt_score: float) -> float:
    base = float(det_conf) * float(ocr_conf)
    return max(0.0, min(1.0, base * (0.7 + 0.3 * fmt_score)))


def _lookup_vehicle(norm: str):
    vehicle = find_vehicle_by_normalized(norm)
    if not vehicle:
        return None
    exp = vehicle.get("guest_expires_at")
    if vehicle.get("is_guest") and exp is not None:
        if isinstance(exp, str):
            exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
        else:
            exp_dt = exp
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
        if exp_dt <= datetime.datetime.now(datetime.timezone.utc):
            return None
    return vehicle


def build_result_row(
    det: dict,
    *,
    direction: str,
    timing: dict | None = None,
    match_status: str | None = None,
    track_confirmed: bool = False,
) -> dict | None:
    """Shape one plate detection for parking_logging (track or legacy pipeline)."""
    _ = direction  # reserved for future direction-specific gates
    plate_text = str(det.get("plate_text") or "").strip()
    norm = str(det.get("plate_normalized") or normalize_plate(plate_text)).strip()
    if not norm:
        return None

    conf = float(det.get("confidence") or 0)
    if conf < plate_ocr_min_confidence():
        return None
    if not is_plausible_plate(plate_text or norm, min_score=plate_format_min_score()):
        return None

    vehicle = _lookup_vehicle(norm)
    row: dict = {
        "plate_text": plate_text or norm,
        "plate_normalized": norm,
        "confidence": conf,
        "box": det.get("box"),
    }
    if det.get("plate_color"):
        row["plate_color"] = str(det.get("plate_color"))
    if timing:
        row["timing"] = dict(timing)
    if track_confirmed:
        row["track_confirmed"] = True
    if match_status:
        row.update(
            {
                "match_status": match_status,
                "vehicle_id": None,
                "is_guest": False,
            }
        )
    elif vehicle:
        row.update(
            {
                "match_status": "registered",
                "vehicle_id": vehicle["id"],
                "is_guest": bool(vehicle.get("is_guest")),
                "owner_name": vehicle.get("owner_name"),
            }
        )
    else:
        row.update(
            {
                "match_status": "unregistered",
                "vehicle_id": None,
                "is_guest": False,
            }
        )
    return row


def detect_plates_in_image(image_path: str) -> list[dict]:
    """
    Return list of detections: plate_text, plate_normalized, confidence, box {x,y,w,h}.
    Uses fast-alpr (YOLO) + production ensemble OCR (global plate model + EasyOCR en/ar).
    """
    alpr = get_alpr()
    if alpr is None:
        err = alpr_init_error() or "ALPR engine not initialized"
        logger.error("Plate detection skipped: %s", err)
        return []

    try:
        alpr_results = alpr.predict(image_path)
    except Exception:
        logger.exception("Plate detection failed for %s", image_path)
        return []

    frame = cv2.imread(image_path)

    if plate_debug_logging():
        logger.info("Plate scan %s: %s raw detection(s)", image_path, len(alpr_results))

    detections: list[dict] = []
    for result in alpr_results:
        ocr = result.ocr
        det_conf = float(result.detection.confidence)

        if ocr is None or not ocr.text:
            if plate_debug_logging():
                logger.info("Plate rejected: det_conf=%.2f reason=no_ocr_text", det_conf)
            continue

        raw_text = ocr.text.strip()
        plate_text = clean_plate_ocr_text(raw_text)
        fmt_score = plate_format_score(plate_text)
        ocr_conf = ocr_confidence_value(ocr.confidence)
        confidence = _combined_confidence(det_conf, ocr_conf, fmt_score)

        if not is_plausible_plate(plate_text, min_score=plate_format_min_score()):
            if plate_debug_logging():
                logger.info(
                    "Plate rejected: raw=%r fixed=%r det=%.2f ocr=%.2f fmt=%.2f reason=format",
                    raw_text,
                    plate_text,
                    det_conf,
                    ocr_conf,
                    fmt_score,
                )
            continue

        bbox = result.detection.bounding_box
        x1, y1, x2, y2 = int(bbox.x1), int(bbox.y1), int(bbox.x2), int(bbox.y2)
        box = {"x": x1, "y": y1, "w": max(0, x2 - x1), "h": max(0, y2 - y1)}
        plate_color = detect_plate_color_from_frame(frame, box) if frame is not None else "unknown"

        detections.append(
            {
                "plate_text": plate_text,
                "plate_normalized": normalize_plate(plate_text),
                "confidence": confidence,
                "box": box,
                "plate_color": plate_color,
            }
        )
        if plate_debug_logging():
            logger.info(
                "Plate accepted: raw=%r fixed=%r det=%.2f ocr=%.2f fmt=%.2f conf=%.2f color=%s",
                raw_text,
                plate_text,
                det_conf,
                ocr_conf,
                fmt_score,
                confidence,
                plate_color,
            )

    if plate_debug_logging() and not detections:
        logger.info("Plate scan %s: no plausible plates after OCR/filter", image_path)

    return detections


def run_plate_detect_on_file(image_path: str, *, direction: str) -> dict:
    """Full check: detect plates, match vehicles, shape for parking_logging."""
    detections = detect_plates_in_image(image_path)
    min_conf = plate_ocr_min_confidence()
    results: list[dict] = []

    for det in detections:
        conf = float(det.get("confidence") or 0)
        if conf < min_conf:
            if plate_debug_logging():
                logger.info(
                    "Plate below confidence gate: %r conf=%.2f min=%.2f",
                    det.get("plate_text"),
                    conf,
                    min_conf,
                )
            continue
        norm = det.get("plate_normalized") or normalize_plate(det.get("plate_text"))
        if not norm:
            continue
        vehicle = _lookup_vehicle(norm)
        row = {
            "plate_text": det.get("plate_text") or norm,
            "plate_normalized": norm,
            "confidence": conf,
            "box": det.get("box"),
        }
        if det.get("plate_color"):
            row["plate_color"] = det.get("plate_color")
        if vehicle:
            row.update(
                {
                    "match_status": "registered",
                    "vehicle_id": vehicle["id"],
                    "is_guest": bool(vehicle.get("is_guest")),
                    "owner_name": vehicle.get("owner_name"),
                }
            )
        else:
            row.update(
                {
                    "match_status": "unregistered",
                    "vehicle_id": None,
                    "is_guest": False,
                }
            )
        results.append(row)

    payload = {
        "status": "ok",
        "direction": direction,
        "plates_detected": len(results),
        "results": results,
    }
    if plate_debug_logging() and len(results) > 1:
        logger.info(
            "Multi-plate frame: %s accepted plate(s): %s",
            len(results),
            [r.get("plate_normalized") for r in results],
        )
    return payload
