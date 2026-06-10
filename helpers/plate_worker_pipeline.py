import logging
from typing import BinaryIO

from database.logs_db import log_software_event
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
) -> dict:
    gate = direction if direction in ("entry", "exit") else gate_direction()
    with light_profile_scope(light_profile):
        result = run_plate_detect_on_file(frame_path, direction=gate)
    try:
        log_parking_events_for_results(frame_path, result)
    except Exception as exc:
        logging.exception("parking log persistence failed")
        log_software_event(
            level="ERROR",
            event="parking.log.persist_failed",
            module="helpers.plate_worker_pipeline",
            message=str(exc),
        )
    return result
