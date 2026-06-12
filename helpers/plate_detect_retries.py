"""Limited retries for plate inference failures."""

from __future__ import annotations

import os
import time

PLATE_DETECT_MAX_RETRIES = max(0, int(os.environ.get("PLATE_DETECT_MAX_RETRIES", "2")))


def detect_with_retries(detect_fn, *args, **kwargs):
    """Call detect_fn until success or retries exhausted. detect_fn returns dict|None."""
    attempts = PLATE_DETECT_MAX_RETRIES + 1
    last = None
    for attempt in range(attempts):
        last = detect_fn(*args, **kwargs)
        if last is not None:
            return last
        if attempt + 1 < attempts:
            time.sleep(0.15 * (attempt + 1))
    return last
