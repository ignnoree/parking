"""Server camera — drainer + stream + plate detection queue."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from io import BytesIO

import cv2
import numpy as np

from helpers.utils import UPLOAD_FOLDER
from helpers.plate_worker_pipeline import run_plate_detect_on_file_obj
from helpers.live_frame_buffer import (
    publish_frame,
    set_camera_disconnected,
    update_overlay_from_plate_results,
)
from database.logs_db import log_software_event

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_drainer_thread: threading.Thread | None = None
_stream_thread: threading.Thread | None = None
_detect_thread: threading.Thread | None = None
_detect_queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=1)
_frame_lock = threading.Lock()
_latest_frame: np.ndarray | None = None


def parse_camera_source(raw: str | None) -> int | str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return value


def _is_network_source(source: int | str) -> bool:
    return isinstance(source, str) and source.lower().startswith(("rtsp", "http"))


def _is_file_source(source: int | str) -> bool:
    if isinstance(source, int) or _is_network_source(source):
        return False
    return os.path.isfile(source)


def camera_frame_interval_seconds() -> float:
    return max(0.5, float(os.environ.get("CAMERA_FRAME_INTERVAL_SECONDS", "1.0")))


def camera_reconnect_delay_seconds() -> float:
    return max(1.0, float(os.environ.get("CAMERA_RECONNECT_DELAY_SECONDS", "5.0")))


def live_stream_max_fps() -> float:
    return min(60.0, max(1.0, float(os.environ.get("LIVE_STREAM_MAX_FPS", "15"))))


def _buffer_drain_reads() -> int:
    return max(1, int(os.environ.get("CAMERA_BUFFER_DRAIN", "3")))


def _open_capture(source: int | str) -> cv2.VideoCapture:
    if _is_network_source(source) or _is_file_source(source):
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(source)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        logger.debug("Could not set camera buffer size", exc_info=True)
    return cap


def _set_latest_frame(frame: np.ndarray | None) -> None:
    global _latest_frame
    with _frame_lock:
        _latest_frame = frame


def _copy_latest_frame() -> np.ndarray | None:
    with _frame_lock:
        if _latest_frame is None:
            return None
        return _latest_frame.copy()


def _rewind_capture(cap: cv2.VideoCapture) -> bool:
    try:
        return bool(cap.set(cv2.CAP_PROP_POS_FRAMES, 0) and cap.isOpened())
    except Exception:
        logger.debug("Could not rewind video capture", exc_info=True)
        return False


def _file_frame_delay(cap: cv2.VideoCapture) -> float:
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    if fps > 1.0:
        return 1.0 / fps
    return 1.0 / 30.0


def _read_drainer_burst(cap: cv2.VideoCapture, network: bool) -> tuple[bool, np.ndarray | None]:
    if not network:
        return cap.read()
    latest_ok, latest = False, None
    for _ in range(_buffer_drain_reads()):
        ok, frame = cap.read()
        if ok and frame is not None:
            latest_ok, latest = True, frame
        else:
            break
    return latest_ok, latest


def _run_plate_detect_on_frame(frame: np.ndarray) -> None:
    path = os.path.join(UPLOAD_FOLDER, f"camera_{uuid.uuid4().hex}.jpg")
    try:
        if not cv2.imwrite(path, frame):
            log_software_event(
                level="WARN",
                event="camera.frame.write_failed",
                module="workers.camera_worker",
                message="Failed to write camera frame for plate detection",
                metadata=f"path={path!r}",
            )
            return
        with open(path, "rb") as f:
            result = run_plate_detect_on_file_obj(f, path)
        update_overlay_from_plate_results(frame.shape, result)
    finally:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Failed to remove temp camera frame %s", path, exc_info=True)


def _enqueue_detect(frame: np.ndarray) -> None:
    try:
        _detect_queue.put_nowait(frame.copy())
    except queue.Full:
        try:
            _detect_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _detect_queue.put_nowait(frame.copy())
        except queue.Full:
            pass


def _detect_worker_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            frame = _detect_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if frame is None:
            break
        try:
            _run_plate_detect_on_frame(frame)
        except Exception:
            logger.exception("Plate detect on camera frame failed")
            log_software_event(
                level="ERROR",
                event="camera.plate_detect.failed",
                module="workers.camera_worker",
                message="Plate detect on camera frame failed",
            )


def _drainer_loop(
    stop_event: threading.Event, source: int | str, network: bool, file_source: bool
) -> None:
    reconnect_delay = camera_reconnect_delay_seconds()
    cap: cv2.VideoCapture | None = None
    fail_streak = 0
    while not stop_event.is_set():
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
                cap = None
            _set_latest_frame(None)
            set_camera_disconnected()
            cap = _open_capture(source)
            if not cap.isOpened():
                log_software_event(
                    level="ERROR",
                    event="camera.open.failed",
                    module="workers.camera_worker",
                    message="Failed to open camera",
                    metadata=f"source={source!r}",
                )
                stop_event.wait(reconnect_delay)
                continue
            fail_streak = 0
            logger.info("Camera opened (drainer): %r", source)
        ok, frame = _read_drainer_burst(cap, network)
        if not ok or frame is None:
            if file_source and cap is not None and _rewind_capture(cap):
                fail_streak = 0
                logger.debug("Video file ended; looping %r", source)
                continue
            fail_streak += 1
            if fail_streak >= 30:
                log_software_event(
                    level="WARN",
                    event="camera.stream.lost",
                    module="workers.camera_worker",
                    message="Camera stream read failures; reconnecting",
                    metadata=f"source={source!r} fail_streak={fail_streak}",
                )
                cap.release()
                cap = None
                fail_streak = 0
                stop_event.wait(reconnect_delay)
            else:
                stop_event.wait(0.01)
            continue
        fail_streak = 0
        _set_latest_frame(frame)
        if file_source and cap is not None:
            stop_event.wait(_file_frame_delay(cap))
    if cap is not None:
        cap.release()
    _set_latest_frame(None)


def _stream_publisher_loop(stop_event: threading.Event) -> None:
    interval = camera_frame_interval_seconds()
    pause = 1.0 / live_stream_max_fps()
    last_detect = 0.0
    while not stop_event.is_set():
        frame = _copy_latest_frame()
        if frame is not None:
            publish_frame(frame)
            now = time.monotonic()
            if now - last_detect >= interval:
                last_detect = now
                _enqueue_detect(frame)
        stop_event.wait(pause)


def start_camera_worker_thread() -> threading.Thread:
    global _drainer_thread, _stream_thread, _detect_thread
    if _stream_thread is not None and _stream_thread.is_alive():
        return _stream_thread
    source = parse_camera_source(os.environ.get("CAMERA_URL", "0"))
    if source is None:
        raise RuntimeError("CAMERA_URL is required")
    network = _is_network_source(source)
    file_source = _is_file_source(source)
    _stop_event.clear()
    _set_latest_frame(None)
    _detect_thread = threading.Thread(
        target=_detect_worker_loop, kwargs={"stop_event": _stop_event}, name="plate-detect", daemon=True
    )
    _detect_thread.start()
    _drainer_thread = threading.Thread(
        target=_drainer_loop,
        kwargs={
            "stop_event": _stop_event,
            "source": source,
            "network": network,
            "file_source": file_source,
        },
        name="camera-drainer",
        daemon=True,
    )
    _drainer_thread.start()
    _stream_thread = threading.Thread(
        target=_stream_publisher_loop, kwargs={"stop_event": _stop_event}, name="camera-stream", daemon=True
    )
    _stream_thread.start()
    logger.info(
        "Parking camera worker started source=%r file=%s network=%s",
        source,
        file_source,
        network,
    )
    return _stream_thread
