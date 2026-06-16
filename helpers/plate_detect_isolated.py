"""Plate OCR in persistent child processes — pool for concurrent frame processing.

PLATE_OCR_WORKERS (default 1) controls pool size. Each detect worker thread
gets its own subprocess slot, so N workers process N frames simultaneously.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue as _stdqueue
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _ocr_pool_size() -> int:
    return max(1, int(os.environ.get("PLATE_OCR_WORKERS", "1")))


@dataclass
class _WorkerSlot:
    process: mp.Process
    request_q: mp.Queue
    response_q: mp.Queue


_slots: list[_WorkerSlot] = []
_free_slots: _stdqueue.Queue[_WorkerSlot] = _stdqueue.Queue()
_init_lock = threading.Lock()
_initialized = False


def _worker_loop(request_q: mp.Queue, response_q: mp.Queue) -> None:
    from helpers.plate_alpr import get_alpr
    from helpers.plate_worker_pipeline import run_plate_detect_on_file_obj

    get_alpr()
    logger.info("Plate OCR models loaded in child process (pid=%s)", os.getpid())

    while True:
        item = request_q.get()
        if item is None:
            break
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


def _start_slot() -> _WorkerSlot:
    ctx = mp.get_context("spawn")
    req_q: mp.Queue = ctx.Queue(maxsize=1)
    resp_q: mp.Queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(
        target=_worker_loop,
        args=(req_q, resp_q),
        name="plate-ocr-worker",
        daemon=True,
    )
    proc.start()
    logger.info("Started isolated plate OCR worker (pid=%s)", proc.pid)
    return _WorkerSlot(process=proc, request_q=req_q, response_q=resp_q)


def ensure_started() -> bool:
    """Initialize the OCR worker pool on first call; no-op if already running."""
    global _slots, _initialized
    if mp.current_process().name != "MainProcess":
        return False
    with _init_lock:
        if _initialized:
            return bool(_slots)
        n = _ocr_pool_size()
        _slots = [_start_slot() for _ in range(n)]
        for slot in _slots:
            _free_slots.put(slot)
        _initialized = True
        logger.info("OCR worker pool ready: %s worker(s)", n)
    return True


def shutdown() -> None:
    global _slots, _initialized
    with _init_lock:
        # Drain the free-slots queue so nothing blocks on put after shutdown.
        while not _free_slots.empty():
            try:
                _free_slots.get_nowait()
            except _stdqueue.Empty:
                break
        for slot in _slots:
            try:
                slot.request_q.put_nowait(None)
            except Exception:
                pass
        for slot in _slots:
            if slot.process.is_alive():
                slot.process.join(timeout=2.0)
            if slot.process.is_alive():
                slot.process.terminate()
                slot.process.join(timeout=1.0)
        _slots = []
        _initialized = False


def is_busy() -> bool:
    n = len(_slots)
    return n > 0 and _free_slots.qsize() < n


def worker_status() -> dict[str, Any]:
    with _init_lock:
        procs = list(_slots)
    alive = [s for s in procs if s.process.is_alive()]
    return {
        "pid": alive[0].process.pid if alive else None,
        "alive": bool(alive),
        "busy": is_busy(),
        "workers": len(procs),
        "workers_alive": len(alive),
    }


def wait_until_idle(timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        n = len(_slots)
        if n == 0 or _free_slots.qsize() >= n:
            return True
        time.sleep(0.05)
    return _free_slots.qsize() >= len(_slots)


def detect_frame_isolated(
    path: str,
    *,
    direction: str,
    light_profile: str,
    frame_shape: tuple[int, ...],
    timeout: float = 120.0,
    skip_logging: bool = False,
) -> dict[str, Any] | None:
    """Run OCR in a child process from the pool.

    Multiple callers can run concurrently — each gets its own slot from the
    pool (sized by PLATE_OCR_WORKERS). Blocks only if all slots are busy.
    """
    if not ensure_started():
        return None

    slot: _WorkerSlot | None = None
    try:
        try:
            slot = _free_slots.get(timeout=min(5.0, timeout))
        except _stdqueue.Empty:
            logger.error("All OCR workers busy — no free slot for %s", path)
            return None

        # Restart a dead worker in-place so the pool stays at its target size.
        if not slot.process.is_alive():
            logger.warning("OCR worker (pid=%s) died; restarting slot", slot.process.pid)
            new_slot = _start_slot()
            with _init_lock:
                idx = next((i for i, s in enumerate(_slots) if s is slot), -1)
                if idx >= 0:
                    _slots[idx] = new_slot
                else:
                    _slots.append(new_slot)
            slot = new_slot

        job_id = os.path.basename(path)
        try:
            slot.request_q.put(
                (job_id, path, direction, light_profile, frame_shape, bool(skip_logging)),
                timeout=5.0,
            )
        except Exception:
            logger.exception("Failed to submit plate detect to child process")
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            return None

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = slot.response_q.get(timeout=0.25)
            except _stdqueue.Empty:
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

    finally:
        if slot is not None:
            _free_slots.put(slot)
