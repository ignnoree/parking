import logging
import time
from typing import BinaryIO

from database.logs_db import log_software_event
from helpers.lighting_monitor import note_plate_scan
from helpers.light_profile import resolve_light_profile
from helpers.plate_pipeline import run_plate_detect_on_file
from helpers.parking_logging import log_parking_events_for_results
from helpers.plate_ocr_preprocess import light_profile_scope
from helpers.utils import gate_direction


def run_plate_detect_on_file_obj(
    file_obj: BinaryIO,
    frame_path: str,
    *,
    direction: str | None = None,
    light_profile: str = "normal",
    skip_logging: bool = False,
) -> dict:
    """
    Run YOLO + OCR on a saved frame.

    Always returns the detection payload. By default, parking events are also
    written to the DB here. When `skip_logging=True`, the caller (e.g. the
    tracker pipeline in the camera worker) takes over the logging decision so
    multiple frames of the same car can be voted on first.
    """
    gate = direction if direction in ("entry", "exit") else gate_direction()
    effective_profile = resolve_light_profile(light_profile)
    wrap_started = time.monotonic()
    with light_profile_scope(effective_profile):
        result = run_plate_detect_on_file(frame_path, direction=gate)

    if isinstance(result, dict):
        result.setdefault("wrap_started_at", wrap_started)

    plates_logged = int(result.get("plates_detected") or 0) if isinstance(result, dict) else 0
    if not skip_logging:
        note_plate_scan(light_profile=effective_profile, plates_logged=plates_logged)
        try:
            logged_plates = log_parking_events_for_results(
                frame_path,
                result,
                wrap_started_at=wrap_started,
            )
            if logged_plates:
                result["logged_plates"] = logged_plates
        except Exception as exc:
            logging.exception("parking log persistence failed")
            log_software_event(
                level="ERROR",
                event="parking.log.persist_failed",
                module="helpers.plate_worker_pipeline",
                message=str(exc),
            )
    return result
