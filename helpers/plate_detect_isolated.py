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
_detect_call_lock = threading.Lock()
_busy = threading.Event()


def _worker_loop(request_q: mp.Queue, response_q: mp.Queue) -> None:
    from helpers.plate_alpr import get_alpr
    from helpers.plate_worker_pipeline import run_plate_detect_on_file_obj

    get_alpr()
    logger.info("Plate OCR models loaded in child process (pid=%s)", os.getpid())

    while True:
        item = request_q.get()
        if item is None:
            break
        # Backward compatible: older callers send 5-tuple without skip_logging.
        if len(item) == 6:
            job_id, path, direction, light_profile, frame_shape, skip_logging = item
        else:
            job_id, path, direction, light_profile, frame_shape = item
            skip_logging = False
        keep_file = bool(skip_logging)
        try:
            with open(path, "rb") as f:
                result = run_plate_detect_on_file_obj(
                    f,
                    path,
                    direction=direction,
                    light_profile=light_profile,
                    skip_logging=skip_logging,
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
            if not keep_file and os.path.isfile(path):
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


def worker_status() -> dict[str, Any]:
    with _lock:
        proc = _process
        pid = proc.pid if proc is not None else None
        alive = proc is not None and proc.is_alive()
    return {"pid": pid, "alive": alive, "busy": is_busy()}


def wait_until_idle(timeout: float = 60.0) -> bool:
    """Wait for in-flight OCR before camera reload (avoids orphaned detect threads)."""
    deadline = time.monotonic() + max(0.5, timeout)
    while is_busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not is_busy()


def detect_frame_isolated(
    path: str,
    *,
    direction: str,
    light_profile: str,
    frame_shape: tuple[int, ...],
    timeout: float = 120.0,
    skip_logging: bool = False,
) -> dict[str, Any] | None:
    """Run OCR in child process; releases GIL while waiting on the response queue.

    When skip_logging=True, the child returns the OCR payload without writing
    to the parking log and leaves the source frame on disk for the caller to
    use (e.g. for tracker-based deferred logging).
    """
    if not ensure_started():
        return None

    job_id = os.path.basename(path)
    with _detect_call_lock:
        _busy.set()
        try:
            assert _request_q is not None and _response_q is not None
            _request_q.put(
                (job_id, path, direction, light_profile, frame_shape, bool(skip_logging)),
                timeout=5.0,
            )

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
