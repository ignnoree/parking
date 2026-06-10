"""Plate OCR in a persistent child process so preview/drainer threads are not GIL-starved."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_process: mp.Process | None = None
_request_q: mp.Queue | None = None
_response_q: mp.Queue | None = None
_lock = threading.Lock()
_busy = threading.Event()


def _worker_loop(request_q: mp.Queue, response_q: mp.Queue) -> None:
    from helpers.plate_alpr import get_alpr
    from helpers.plate_worker_pipeline import run_plate_detect_on_file_obj

    get_alpr()

    while True:
        item = request_q.get()
        if item is None:
            break
        job_id, path, direction, light_profile, frame_shape = item
        try:
            with open(path, "rb") as f:
                result = run_plate_detect_on_file_obj(
                    f,
                    path,
                    direction=direction,
                    light_profile=light_profile,
                )
            response_q.put(
                {"job_id": job_id, "ok": True, "result": result, "frame_shape": frame_shape}
            )
        except Exception as exc:
            logger.exception("Plate detect child process failed")
            response_q.put(
                {"job_id": job_id, "ok": False, "error": str(exc), "frame_shape": frame_shape}
            )
        finally:
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def ensure_started() -> bool:
    global _process, _request_q, _response_q
    if mp.current_process().name != "MainProcess":
        return False
    with _lock:
        if _process is not None and _process.is_alive():
            return True
        ctx = mp.get_context("spawn")
        _request_q = ctx.Queue(maxsize=1)
        _response_q = ctx.Queue(maxsize=1)
        _process = ctx.Process(
            target=_worker_loop,
            args=(_request_q, _response_q),
            name="plate-ocr-worker",
            daemon=True,
        )
        _process.start()
        logger.info("Started isolated plate OCR worker (pid=%s)", _process.pid)
        return True


def shutdown() -> None:
    global _process, _request_q, _response_q
    with _lock:
        if _request_q is not None:
            try:
                _request_q.put_nowait(None)
            except Exception:
                pass
        if _process is not None and _process.is_alive():
            _process.join(timeout=2.0)
            if _process.is_alive():
                _process.terminate()
                _process.join(timeout=1.0)
        _process = None
        _request_q = None
        _response_q = None
    _busy.clear()


def is_busy() -> bool:
    return _busy.is_set()


def detect_frame_isolated(
    path: str,
    *,
    direction: str,
    light_profile: str,
    frame_shape: tuple[int, ...],
    timeout: float = 120.0,
) -> dict[str, Any] | None:
    """Run OCR in child process; releases GIL while waiting on the response queue."""
    if not ensure_started():
        return None

    job_id = os.path.basename(path)
    _busy.set()
    try:
        assert _request_q is not None and _response_q is not None
        _request_q.put((job_id, path, direction, light_profile, frame_shape), timeout=5.0)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = _response_q.get(timeout=0.25)
            except queue.Empty:
                continue
            if msg.get("job_id") != job_id:
                continue
            if not msg.get("ok"):
                logger.error("Plate detect child failed: %s", msg.get("error"))
                return None
            result = msg.get("result")
            return result if isinstance(result, dict) else None
        logger.error("Plate detect child timed out for %s", path)
        return None
    except Exception:
        logger.exception("Failed to submit plate detect to child process")
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return None
    finally:
        _busy.clear()
