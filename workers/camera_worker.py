"""Server camera — drainer + stream + plate detection queue (DB-driven)."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass

import cv2
import numpy as np

from database.cameras_db import (
    default_frame_interval,
    list_cameras,
    parse_camera_source,
)
from database.logs_db import log_software_event
from helpers.live_frame_buffer import (
    publish_frame,
    set_camera_disconnected,
    update_overlay_from_plate_results,
)
from helpers.plate_detect_isolated import detect_frame_isolated, is_busy
from helpers.utils import UPLOAD_FOLDER

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraConfig:
    id: int
    name: str
    source: int | str
    gate_role: str
    light_profile: str
    frame_interval_seconds: float
    network: bool
    file_source: bool


@dataclass
class _DetectJob:
    frame: np.ndarray
    camera_id: int
    direction: str
    light_profile: str


class _WorkerRuntime:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.primary_camera_id: int | None = None
        self.configs: list[CameraConfig] = []
        self.drainer_threads: list[threading.Thread] = []
        self.preview_thread: threading.Thread | None = None
        self.detect_thread: threading.Thread | None = None
        self.detect_queue: queue.Queue[_DetectJob | None] = queue.Queue(maxsize=1)

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.drainer_threads)


_runtime: _WorkerRuntime | None = None
_runtime_lock = threading.Lock()
_reload_lock = threading.Lock()
_frame_lock = threading.Lock()
_frame_ready = threading.Condition(_frame_lock)
_latest_frame: np.ndarray | None = None
_frame_generation: int = 0


def _is_network_source(source: int | str) -> bool:
    return isinstance(source, str) and source.lower().startswith(("rtsp", "http"))


def _is_file_source(source: int | str) -> bool:
    if isinstance(source, int) or _is_network_source(source):
        return False
    return os.path.isfile(source)


def camera_reconnect_delay_seconds() -> float:
    return max(1.0, float(os.environ.get("CAMERA_RECONNECT_DELAY_SECONDS", "5.0")))


def live_stream_max_fps() -> float:
    return min(60.0, max(1.0, float(os.environ.get("LIVE_STREAM_MAX_FPS", "15"))))


def _buffer_drain_reads() -> int:
    return max(1, int(os.environ.get("CAMERA_BUFFER_DRAIN", "3")))


def _open_capture(source: int | str) -> cv2.VideoCapture:
    if (
        _is_network_source(source)
        or _is_file_source(source)
        or (isinstance(source, str) and os.path.isfile(source))
    ):
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(source)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        logger.debug("Could not set camera buffer size", exc_info=True)
    return cap


def _set_latest_frame(frame: np.ndarray) -> None:
    global _latest_frame, _frame_generation
    with _frame_ready:
        _latest_frame = frame
        _frame_generation += 1
        _frame_ready.notify_all()


def _clear_latest_frame() -> None:
    global _latest_frame, _frame_generation
    with _frame_ready:
        _latest_frame = None
        _frame_generation += 1
        _frame_ready.notify_all()


def _preview_target_fps(cap: cv2.VideoCapture, *, file_source: bool) -> float:
    target_fps = live_stream_max_fps()
    if file_source:
        video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if video_fps >= 10.0:
            target_fps = min(target_fps, video_fps)
    return max(1.0, target_fps)


def _rewind_capture(cap: cv2.VideoCapture) -> bool:
    try:
        return bool(cap.set(cv2.CAP_PROP_POS_FRAMES, 0) and cap.isOpened())
    except Exception:
        logger.debug("Could not rewind video capture", exc_info=True)
        return False


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


def _config_from_row(row: dict) -> CameraConfig | None:
    source = parse_camera_source(row["protocol"], row["source"])
    if source is None:
        return None
    interval = row.get("frame_interval_seconds")
    if interval is None:
        interval = default_frame_interval()
    else:
        interval = max(0.5, float(interval))
    return CameraConfig(
        id=int(row["id"]),
        name=str(row.get("name") or f"Camera {row['id']}"),
        source=source,
        gate_role=str(row.get("gate_role") or "entry"),
        light_profile=str(row.get("light_profile") or "normal"),
        frame_interval_seconds=interval,
        network=_is_network_source(source),
        file_source=_is_file_source(source)
        or (isinstance(source, str) and os.path.isfile(source)),
    )


def load_camera_configs() -> list[CameraConfig]:
    configs: list[CameraConfig] = []
    for row in list_cameras(enabled_only=True):
        cfg = _config_from_row(row)
        if cfg is not None:
            configs.append(cfg)
    return configs


def _warn_env_camera_url_mismatch() -> None:
    env_url = os.environ.get("CAMERA_URL", "").strip()
    if not env_url:
        return
    cameras = list_cameras(enabled_only=True)
    if len(cameras) != 1:
        return
    row = cameras[0]
    db_source = str(row.get("source") or "").strip()
    if db_source != env_url:
        logger.warning(
            "CAMERA_URL in .env (%r) differs from database camera id=%s (%r). "
            "The database value is used — change it in /admin (or reset DB to re-seed from .env).",
            env_url,
            row["id"],
            db_source,
        )


def _run_plate_detect_on_frame(job: _DetectJob) -> None:
    path = os.path.join(UPLOAD_FOLDER, f"camera_{job.camera_id}_{uuid.uuid4().hex}.jpg")
    if not cv2.imwrite(path, job.frame):
        log_software_event(
            level="WARN",
            event="camera.frame.write_failed",
            module="workers.camera_worker",
            message="Failed to write camera frame for plate detection",
            metadata=f"path={path!r} camera_id={job.camera_id}",
        )
        return

    result = detect_frame_isolated(
        path,
        direction=job.direction,
        light_profile=job.light_profile,
        frame_shape=job.frame.shape[:2],
    )
    if result and _runtime and job.camera_id == _runtime.primary_camera_id:
        update_overlay_from_plate_results(job.frame.shape, result)


def _enqueue_detect(job: _DetectJob, detect_queue: queue.Queue[_DetectJob | None]) -> None:
    """Skip if OCR child is still busy — preview must not wait for plates."""
    if is_busy():
        return
    while True:
        try:
            detect_queue.get_nowait()
        except queue.Empty:
            break
    try:
        detect_queue.put_nowait(job)
    except queue.Full:
        pass


def _detect_worker_loop(stop_event: threading.Event, detect_queue: queue.Queue[_DetectJob | None]) -> None:
    while not stop_event.is_set():
        try:
            job = detect_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if job is None:
            break
        try:
            _run_plate_detect_on_frame(job)
        except Exception:
            logger.exception("Plate detect on camera frame failed (camera_id=%s)", job.camera_id)
            log_software_event(
                level="ERROR",
                event="camera.plate_detect.failed",
                module="workers.camera_worker",
                message="Plate detect on camera frame failed",
                metadata=f"camera_id={job.camera_id}",
            )


def _preview_publisher_loop(stop_event: threading.Event) -> None:
    """Encode/publish on its own thread; drainer only decodes video frames."""
    pause = 1.0 / live_stream_max_fps()
    last_generation = -1
    while not stop_event.is_set():
        with _frame_ready:
            generation = _frame_generation
            frame = None if _latest_frame is None else _latest_frame.copy()
        if frame is not None and generation != last_generation:
            publish_frame(frame)
            last_generation = generation
        stop_event.wait(pause)


def _drainer_loop(
    stop_event: threading.Event,
    config: CameraConfig,
    detect_queue: queue.Queue[_DetectJob | None],
    *,
    is_primary: bool,
) -> None:
    reconnect_delay = camera_reconnect_delay_seconds()
    cap: cv2.VideoCapture | None = None
    fail_streak = 0
    last_detect = 0.0
    next_frame_at = 0.0
    while not stop_event.is_set():
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
                cap = None
            if is_primary:
                _clear_latest_frame()
                set_camera_disconnected()
            cap = _open_capture(config.source)
            if not cap.isOpened():
                log_software_event(
                    level="ERROR",
                    event="camera.open.failed",
                    module="workers.camera_worker",
                    message="Failed to open camera",
                    metadata=f"camera_id={config.id} source={config.source!r}",
                )
                stop_event.wait(reconnect_delay)
                continue
            fail_streak = 0
            next_frame_at = time.monotonic()
            logger.info("Camera opened: id=%s name=%r source=%r", config.id, config.name, config.source)

        ok, frame = _read_drainer_burst(cap, config.network)
        if not ok or frame is None:
            if config.file_source and cap is not None and _rewind_capture(cap):
                fail_streak = 0
                next_frame_at = time.monotonic()
                logger.debug("Video file ended; looping camera_id=%s", config.id)
                continue
            fail_streak += 1
            if fail_streak >= 30:
                log_software_event(
                    level="WARN",
                    event="camera.stream.lost",
                    module="workers.camera_worker",
                    message="Camera stream read failures; reconnecting",
                    metadata=f"camera_id={config.id} fail_streak={fail_streak}",
                )
                cap.release()
                cap = None
                fail_streak = 0
                stop_event.wait(reconnect_delay)
            else:
                stop_event.wait(0.01)
            continue

        fail_streak = 0
        if is_primary:
            _set_latest_frame(frame)

        now = time.monotonic()
        if now - last_detect >= config.frame_interval_seconds:
            last_detect = now
            _enqueue_detect(
                _DetectJob(
                    frame=frame.copy(),
                    camera_id=config.id,
                    direction=config.gate_role,
                    light_profile=config.light_profile,
                ),
                detect_queue,
            )

        if cap is not None:
            frame_interval = 1.0 / _preview_target_fps(cap, file_source=config.file_source)
            next_frame_at += frame_interval
            sleep_for = next_frame_at - time.monotonic()
            if sleep_for > 0:
                stop_event.wait(sleep_for)
            else:
                next_frame_at = time.monotonic()

    if cap is not None:
        cap.release()


def _stop_runtime(runtime: _WorkerRuntime) -> None:
    runtime.stop_event.set()
    try:
        runtime.detect_queue.put_nowait(None)
    except queue.Full:
        try:
            runtime.detect_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            runtime.detect_queue.put_nowait(None)
        except queue.Full:
            pass
    for thread in runtime.drainer_threads:
        thread.join(timeout=2.0)
    if runtime.preview_thread:
        runtime.preview_thread.join(timeout=2.0)
    if runtime.detect_thread:
        runtime.detect_thread.join(timeout=0.5)
    _clear_latest_frame()
    set_camera_disconnected()


def _start_runtime(configs: list[CameraConfig]) -> _WorkerRuntime:
    runtime = _WorkerRuntime()
    runtime.configs = configs
    runtime.primary_camera_id = configs[0].id if configs else None
    runtime.stop_event.clear()
    _clear_latest_frame()

    runtime.preview_thread = threading.Thread(
        target=_preview_publisher_loop,
        kwargs={"stop_event": runtime.stop_event},
        name="camera-preview",
        daemon=True,
    )
    runtime.preview_thread.start()

    runtime.detect_thread = threading.Thread(
        target=_detect_worker_loop,
        kwargs={"stop_event": runtime.stop_event, "detect_queue": runtime.detect_queue},
        name="plate-detect",
        daemon=True,
    )
    runtime.detect_thread.start()

    for idx, cfg in enumerate(configs):
        thread = threading.Thread(
            target=_drainer_loop,
            kwargs={
                "stop_event": runtime.stop_event,
                "config": cfg,
                "detect_queue": runtime.detect_queue,
                "is_primary": idx == 0,
            },
            name=f"camera-drainer-{cfg.id}",
            daemon=True,
        )
        thread.start()
        runtime.drainer_threads.append(thread)

    return runtime


def get_worker_status() -> dict:
    with _runtime_lock:
        runtime = _runtime
    cameras = []
    if runtime:
        for cfg in runtime.configs:
            cameras.append(
                {
                    "id": cfg.id,
                    "name": cfg.name,
                    "gate_role": cfg.gate_role,
                    "light_profile": cfg.light_profile,
                    "is_primary": cfg.id == runtime.primary_camera_id,
                }
            )
    return {
        "running": runtime is not None and runtime.alive(),
        "camera_count": len(cameras),
        "cameras": cameras,
        "primary_camera_id": runtime.primary_camera_id if runtime else None,
        "ocr_busy": is_busy(),
    }


def _reload_cameras_sync() -> None:
    global _runtime
    _warn_env_camera_url_mismatch()
    configs = load_camera_configs()
    with _runtime_lock:
        if _runtime is not None:
            _stop_runtime(_runtime)
            _runtime = None
        if not configs:
            set_camera_disconnected()
            logger.warning("No enabled cameras configured in database")
            return
        _runtime = _start_runtime(configs)
    logger.info("Camera worker reloaded with %s camera(s)", len(configs))


def reload_cameras() -> None:
    """Reload enabled cameras from DB without blocking the HTTP response."""
    def _run() -> None:
        with _reload_lock:
            _reload_cameras_sync()

    threading.Thread(target=_run, name="camera-reload", daemon=True).start()


def start_camera_worker_thread() -> threading.Thread | None:
    """Start workers for all enabled DB cameras (env bootstrap runs at app init)."""
    global _runtime
    _warn_env_camera_url_mismatch()
    with _runtime_lock:
        if _runtime is not None and _runtime.alive():
            return _runtime.drainer_threads[0] if _runtime.drainer_threads else None
        configs = load_camera_configs()
        if not configs:
            set_camera_disconnected()
            logger.warning("No enabled cameras — camera worker not started")
            return None
        _runtime = _start_runtime(configs)
        logger.info(
            "Parking camera worker started with %s camera(s); primary id=%s",
            len(configs),
            _runtime.primary_camera_id,
        )
        return _runtime.drainer_threads[0] if _runtime.drainer_threads else None
