import logging
import os
import threading
import time

from database.plates_db import list_expired_guest_plate_ids, soft_delete_plate
from database.logs_db import log_software_event

logger = logging.getLogger(__name__)


def guest_retention_days() -> int:
    return max(1, int(os.environ.get("GUEST_RETENTION_DAYS", "30")))


def purge_expired_guest_vehicles() -> int:
    purged = 0
    for pid in list_expired_guest_plate_ids():
        if soft_delete_plate(plate_id=pid):
            purged += 1
            log_software_event(
                level="INFO",
                event="guest.expired",
                module="helpers.guest_expiry",
                message="Guest plate expired and soft-deleted",
                metadata=f"plate_id={pid}",
            )
    if purged:
        logger.info("Purged %s expired guest plate(s)", purged)
    return purged


def start_guest_expiry_thread() -> threading.Thread:
    def _loop() -> None:
        while True:
            try:
                purge_expired_guest_vehicles()
            except Exception as exc:
                logger.exception("Guest expiry purge failed")
                log_software_event(
                    level="ERROR",
                    event="guest.expiry.purge_failed",
                    module="helpers.guest_expiry",
                    message=str(exc),
                )
            time.sleep(3600)

    t = threading.Thread(target=_loop, name="guest-expiry", daemon=True)
    t.start()
    return t
