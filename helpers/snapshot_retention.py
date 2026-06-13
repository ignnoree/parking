"""Purge parking log snapshot files older than the configured retention window."""

from __future__ import annotations

import logging
import os
import threading
import time

from database.logs_db import log_software_event
from helpers.utils import (
    PARKING_KNOWN_CROP_FOLDER,
    PARKING_KNOWN_SOURCE_FOLDER,
    PARKING_UNKNOWN_CROP_FOLDER,
    PARKING_UNKNOWN_SOURCE_FOLDER,
)

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400


def snapshot_retention_days() -> int:
    """Default 90 days (~3 months). Set SNAPSHOT_RETENTION_DAYS=0 to disable purges."""
    return max(0, int(os.environ.get("SNAPSHOT_RETENTION_DAYS", "90")))


def snapshot_retention_purge_interval_seconds() -> int:
    return max(3600, int(os.environ.get("SNAPSHOT_RETENTION_PURGE_INTERVAL_SECONDS", "86400")))


def _snapshot_folders() -> tuple[str, ...]:
    return (
        PARKING_KNOWN_SOURCE_FOLDER,
        PARKING_KNOWN_CROP_FOLDER,
        PARKING_UNKNOWN_SOURCE_FOLDER,
        PARKING_UNKNOWN_CROP_FOLDER,
    )


def purge_old_snapshots() -> int:
    """Delete parking log source/crop JPEGs older than the retention window."""
    days = snapshot_retention_days()
    if days <= 0:
        return 0

    cutoff = time.time() - (days * _SECONDS_PER_DAY)
    removed = 0
    for folder in _snapshot_folders():
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
                os.remove(path)
                removed += 1
            except OSError:
                logger.debug("Failed to remove old snapshot %s", path, exc_info=True)

    if removed:
        logger.info(
            "Purged %s parking snapshot file(s) older than %s day(s)",
            removed,
            days,
        )
        log_software_event(
            level="INFO",
            event="snapshot.retention.purged",
            module="helpers.snapshot_retention",
            message=f"Purged {removed} snapshot file(s)",
            metadata=f"retention_days={days}",
        )
    return removed


def start_snapshot_retention_thread() -> threading.Thread:
    def _loop() -> None:
        while True:
            try:
                purge_old_snapshots()
            except Exception as exc:
                logger.exception("Snapshot retention purge failed")
                log_software_event(
                    level="ERROR",
                    event="snapshot.retention.purge_failed",
                    module="helpers.snapshot_retention",
                    message=str(exc),
                )
            time.sleep(snapshot_retention_purge_interval_seconds())

    t = threading.Thread(target=_loop, name="snapshot-retention", daemon=True)
    t.start()
    return t
