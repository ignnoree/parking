"""Server camera — drainer + stream + plate detection queue (DB-driven)."""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field

import cv2
import numpy as np

from database.cameras_db import (
    default_frame_interval,
    list_cameras,
    parse_camera_source,
)
from database.logs_db import log_software_event
from helpers.lighting_monitor import note_plate_scan
from helpers.live_frame_buffer import (
    publish_frame,
    set_camera_disconnected,
)
from helpers.parking_logging import log_parking_events_for_results, log_uncertain_track_event
from helpers.plate_detect_isolated import (
    detect_frame_isolated,
    is_busy,
    wait_until_idle,
    worker_status,
)
from helpers.plate_detect_retries import detect_with_retries
from helpers.plate_pipeline import build_result_row, plate_debug_logging
from helpers.plate_tracker import (
    PlateTracker,
    TrackLogDecision,
    box_for_log,
    plate_track_min_hits,
    plate_tracking_enabled,
)
from helpers.light_profile import resolve_light_profile
from helpers.utils import UPLOAD_TEMP_FOLDER
from helpers.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)


def _track_frame_dest(camera_id: uuid.UUID, track_id: int) -> str:
    """Stable temp path for the most-recent OCR frame belonging to a track."""
    return os.path.join(UPLOAD_TEMP_FOLDER, f"trackframe_{camera_id}_{track_id}.jpg")


def _save_track_frame(src: str, camera_id: uuid.UUID, track_id: int) -> None:
    """Copy the detection frame as the track snapshot only if it is the best-confidence frame so far."""
    dest = _track_frame_dest(camera_id, track_id)
    try:
        shutil.copy2(src, dest)
    except OSError:
        logger.debug("Could not save track frame cam=%s track=%s", camera_id, track_id)


def _cleanup_track_frames(camera_ids: list[uuid.UUID]) -> None:
    """Delete leftover per-track frame temp files for the given cameras."""
    for cam_id in camera_ids:
        prefix = f"trackframe_{cam_id}_"
        try:
            for fname in os.listdir(UPLOAD_TEMP_FOLDER):
                if fname.startswith(prefix) and fname.endswith(".jpg"):
                    _remove_temp_file(os.path.join(UPLOAD_TEMP_FOLDER, fname))
        except OSError:
            logger.debug("Could not list temp folder for track-frame cleanup cam=%s", cam_id)


def _pop_track_frame(camera_id: uuid.UUID, track_id: int) -> str | None:
    """Return the saved track-frame path if it exists (caller must delete after use)."""
    path = _track_frame_dest(camera_id, track_id)
    return path if os.path.exists(path) else None


@dataclass(frozen=True)
class CameraConfig:
    id: uuid.UUID
    name: str
    source: int | str
    direction: str
    light_profile: str
    frame_interval_seconds: float
    network: bool
    file_source: bool


@dataclass
class _DetectJob:
    frame: np.ndarray
    camera_id: uuid.UUID
    direction: str
    light_profile: str


@dataclass
class _CameraTrackState:
    """Per-camera tracker used for multi-frame voting before logging."""

    tracker: PlateTracker = field(default_factory=PlateTracker)
    # Serializes tracker mutations when multiple detect threads run concurrently.
    process_lock: threading.Lock = field(default_factory=threading.Lock)


class _WorkerRuntime:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.primary_camera_id: uuid.UUID | None = None
        self.configs: list[CameraConfig] = []
        self.drainer_threads: list[threading.Thread] = []
        self.preview_thread: threading.Thread | None = None
        self.detect_queue: queue.Queue[_DetectJob | None] = queue.Queue(
            maxsize=camera_detect_queue_size()
        )
        self.track_states: dict[uuid.UUID, _CameraTrackState] = {}
        self.track_lock = threading.Lock()
        self.detect_threads: list[threading.Thread] = []

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.drainer_threads)

    def track_state_for(self, camera_id: uuid.UUID) -> _CameraTrackState:
        with self.track_lock:
            state = self.track_states.get(camera_id)
            if state is None:
                state = _CameraTrackState()
                self.track_states[camera_id] = state
            return state


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


def camera_detect_queue_size() -> int:
    return max(1, min(32, int(os.environ.get("CAMERA_DETECT_QUEUE_SIZE", "4"))))


def plate_ocr_workers() -> int:
    return max(1, int(os.environ.get("PLATE_OCR_WORKERS", "1")))


def camera_video_loop_enabled() -> bool:
    """When true, local video file sources rewind to frame 0 after EOF."""
    return os.environ.get("CAMERA_VIDEO_LOOP", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
    """Frame pacing for the drainer loop. File sources play at native video FPS."""
    if file_source:
        video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if video_fps >= 5.0:
            return min(60.0, max(5.0, video_fps))
    return max(1.0, live_stream_max_fps())


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
    cid = parse_uuid(row["id"])
    if cid is None:
        return None
    interval = default_frame_interval()
    return CameraConfig(
        id=cid,
        name=str(row.get("name") or f"Camera {row['id']}"),
        source=source,
        direction=str(row.get("direction") or "entry"),
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


def _remove_temp_file(path: str) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        logger.debug("Failed to remove temp frame %s", path, exc_info=True)


def _route_results_through_tracker(
    *,
    job: _DetectJob,
    frame_path: str,
    result: dict,
    runtime: _WorkerRuntime,
) -> None:
    """Feed the OCR result through the per-camera tracker for multi-frame voting."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return

    raw_results = [r for r in (result.get("results") or []) if isinstance(r, dict)]
    state = runtime.track_state_for(job.camera_id)
    tracker = state.tracker

    # Serialize all tracker mutations per camera. Multiple detect threads run
    # OCR concurrently (each in its own subprocess slot), but the tracker is
    # not thread-safe so updates must be single-threaded per camera.
    with state.process_lock:
        now = time.monotonic()

        def _process_expired_track(track) -> None:
            decision = tracker.resolve_track_log(track, on_expiry=True)
            if decision is None or decision.tier != "uncertain":
                return
            timing = tracker.log_timing_for_track(track, now=now)
            # Use the frame saved at OCR-read time (the scan that actually saw the
            # plate), not frame_path (the current scan that just found no IoU match
            # and triggered expiry — the car is already gone from that frame).
            ocr_frame = _pop_track_frame(job.camera_id, track.track_id) or frame_path
            try:
                log_uncertain_track_event(
                    ocr_frame,
                    direction=job.direction,
                    plate_normalized=str(decision.read.get("plate_normalized") or ""),
                    plate_text=str(decision.read.get("plate_text") or ""),
                    confidence=float(decision.read.get("confidence") or 0),
                    box=box_for_log(decision.read, track) or dict(track.box),
                    timing=timing,
                    skip_reason=decision.reason,
                    track_id=track.track_id,
                    plate_color=str(decision.read.get("plate_color") or "") or None,
                )
            finally:
                if ocr_frame != frame_path:
                    _remove_temp_file(ocr_frame)

        def _process_live_decision(track, decision: TrackLogDecision) -> None:
            if decision.tier != "confirmed":
                return
            timing = tracker.log_timing(track.track_id, now=now)
            # Use the track's current IoU-updated box (matches the current frame)
            # rather than the OCR-read box, which may be from an older scan position.
            log_box = dict(track.box) if track.box.get("w") and track.box.get("h") else box_for_log(decision.read, track)
            row = build_result_row(
                {
                    "plate_text": decision.read.get("plate_text"),
                    "plate_normalized": decision.read.get("plate_normalized"),
                    "confidence": decision.read.get("confidence"),
                    "box": log_box,
                    "plate_color": decision.read.get("plate_color"),
                },
                direction=job.direction,
                timing=timing,
                track_confirmed=True,
            )
            if row is None:
                return

            payload = {
                "status": "ok",
                "direction": job.direction,
                "plates_detected": 1,
                "results": [row],
            }
            try:
                log_parking_events_for_results(frame_path, payload)
            except Exception:
                logger.exception(
                    "Tracker log persistence failed (camera_id=%s track=%s)",
                    job.camera_id,
                    track.track_id,
                )
                return

            tracker.mark_confirmed(
                track.track_id,
                plate_text=str(decision.read.get("plate_text") or ""),
                plate_normalized=str(decision.read.get("plate_normalized") or ""),
                confidence=float(decision.read.get("confidence") or 0),
                logged=True,
            )
            # Confirmed track — we used frame_path (current frame), so the
            # intermediate OCR frame copy for this track is no longer needed.
            _remove_temp_file(_track_frame_dest(job.camera_id, track.track_id))

        # IoU-associate raw detections with active tracks (creates new tracks as needed).
        _need_ocr, expired_tracks = tracker.update(
            [{"box": r.get("box"), "confidence": r.get("confidence")} for r in raw_results if r.get("box")],
            now=now,
        )

        for track in expired_tracks:
            _process_expired_track(track)

        min_hits = plate_track_min_hits()
        for det in raw_results:
            box = det.get("box")
            if not isinstance(box, dict):
                continue
            plate_norm = str(det.get("plate_normalized") or "").strip()
            det_conf = float(det.get("confidence") or 0)
            track = tracker.track_for_box(box, exclude_logged=True)
            orphan_reason: str | None = None
            needs_orphan = False
            if track is not None and plate_norm and not tracker.track_accepts_plate(track, plate_norm):
                needs_orphan = True
                orphan_reason = "plate_mismatch"
            elif track is None:
                needs_orphan = True
                orphan_reason = "no_iou_track"

            if needs_orphan:
                # Before creating the orphan, check whether this read is just the
                # visible suffix of a plate already logged by an IoU-overlapping
                # track (car partially exiting the frame). tracker.update() keeps
                # logged-track boxes current, so the IoU is always fresh.
                if plate_norm and tracker.logged_plate_covers(box, plate_norm):
                    if plate_debug_logging():
                        logger.info(
                            "[TRACKER] cam=%s suppress partial-exit read=%r reason=logged_suffix",
                            job.camera_id,
                            plate_norm,
                        )
                    continue
                track = tracker.create_orphan_track(box, confidence=det_conf, now=now)

            if orphan_reason and plate_debug_logging():
                logger.info(
                    "[TRACKER] cam=%s %s track=%s text=%r conf=%.2f",
                    job.camera_id,
                    orphan_reason,
                    track.track_id,
                    plate_norm,
                    det_conf,
                )

            # OCR-similarity association: if the IoU pass landed on a brand-new
            # track (no reads yet) and another active track has similar OCR text,
            # merge into that track instead. This catches the case where a moving
            # car between scans doesn't overlap its previous box but has a
            # recognizably-similar plate read.
            if (
                not track.ocr_reads
                and not track.logged
                and plate_norm
            ):
                sibling = tracker.track_for_ocr_text(plate_norm, exclude_id=track.track_id)
                if sibling is not None and tracker.merge_into(track.track_id, sibling.track_id):
                    if plate_debug_logging():
                        logger.info(
                            "[TRACKER] cam=%s merge new_track->%s text=%r (OCR-similarity fallback)",
                            job.camera_id,
                            sibling.track_id,
                            plate_norm,
                        )
                    track = sibling

            if track.logged:
                continue
            # Filter single-frame ghost detections: require min_hits before trusting OCR reads.
            if track.hits < min_hits:
                if plate_debug_logging():
                    logger.info(
                        "[TRACKER] cam=%s track=%s skip read=%r reason=min_hits hits=%s need=%s",
                        job.camera_id,
                        track.track_id,
                        det.get("plate_normalized"),
                        track.hits,
                        min_hits,
                    )
                continue

            tracker.mark_ocr_pending(track.track_id, now=now)
            tracker.record_ocr_read(
                track.track_id,
                {
                    "plate_text": det.get("plate_text"),
                    "plate_normalized": det.get("plate_normalized"),
                    "confidence": det.get("confidence"),
                    "plate_color": det.get("plate_color"),
                    "box": dict(box),
                },
            )
            tracker.mark_ocr_finished(track.track_id)
            # Save this frame only when the new read is the best-confidence read so
            # far on this track — the saved file must match the read that
            # resolve_track_log() will select as decision.read for uncertain crops.
            new_conf = float(det.get("confidence") or 0)
            prev_best = max(
                (float(r.get("confidence") or 0) for r in track.ocr_reads[:-1]),
                default=-1.0,
            )
            if new_conf > prev_best:
                _save_track_frame(frame_path, job.camera_id, track.track_id)

            if plate_debug_logging():
                logger.info(
                    "[TRACKER] cam=%s track=%s read=%r conf=%.2f hits=%s attempts=%s",
                    job.camera_id,
                    track.track_id,
                    det.get("plate_normalized"),
                    float(det.get("confidence") or 0),
                    track.hits,
                    track.ocr_attempts,
                )

            live_track = tracker.get_track(track.track_id)
            if live_track is None:
                continue
            decision = tracker.resolve_track_log(live_track, on_expiry=False)
            if decision is None:
                continue

            _process_live_decision(live_track, decision)


def _run_plate_detect_on_frame(job: _DetectJob) -> None:
    path = os.path.join(UPLOAD_TEMP_FOLDER, f"camera_{job.camera_id}_{uuid.uuid4().hex}.jpg")
    if not cv2.imwrite(path, job.frame):
        log_software_event(
            level="WARN",
            event="camera.frame.write_failed",
            module="workers.camera_worker",
            message="Failed to write camera frame for plate detection",
            metadata=f"path={path!r} camera_id={job.camera_id}",
        )
        return

    profile = resolve_light_profile(job.light_profile)
    tracker_on = plate_tracking_enabled() and _runtime is not None
    try:
        result = detect_with_retries(
            detect_frame_isolated,
            path,
            direction=job.direction,
            light_profile=profile,
            frame_shape=job.frame.shape[:2],
            skip_logging=tracker_on,
        )

        if tracker_on and _runtime is not None:
            plates_detected = (
                int(result.get("plates_detected") or 0) if isinstance(result, dict) else 0
            )
            note_plate_scan(light_profile=profile, plates_logged=plates_detected)

            _route_results_through_tracker(
                job=job,
                frame_path=path,
                result=result if isinstance(result, dict) else {},
                runtime=_runtime,
            )
    finally:
        # Tracker mode keeps the file alive across the child call; we always
        # clean it up here so per-frame temp files cannot leak.
        if tracker_on:
            _remove_temp_file(path)


def _enqueue_detect(job: _DetectJob, detect_queue: queue.Queue[_DetectJob | None]) -> None:
    """Queue scan frames; drop oldest when full so crowded scenes are not skipped entirely."""
    try:
        detect_queue.put_nowait(job)
        return
    except queue.Full:
        pass
    try:
        detect_queue.get_nowait()
    except queue.Empty:
        return
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
    video_finished = False
    while not stop_event.is_set():
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
                cap = None
            if config.file_source and not camera_video_loop_enabled() and video_finished:
                stop_event.wait(0.5)
                continue
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
            video_finished = False
            logger.info(
                "Camera opened: id=%s name=%r source=%r scan_interval=%.1fs",
                config.id,
                config.name,
                config.source,
                config.frame_interval_seconds,
            )
            ocr = worker_status()
            if ocr.get("alive"):
                logger.info(
                    "Plate OCR worker ready (pid=%s); models load once — not re-initialized on video change",
                    ocr.get("pid"),
                )

        ok, frame = _read_drainer_burst(cap, config.network)
        if not ok or frame is None:
            if (
                config.file_source
                and cap is not None
                and camera_video_loop_enabled()
                and _rewind_capture(cap)
            ):
                fail_streak = 0
                next_frame_at = time.monotonic()
                logger.debug("Video file ended; looping camera_id=%s", config.id)
                continue
            if config.file_source and cap is not None and not camera_video_loop_enabled():
                logger.info(
                    "Video file finished (CAMERA_VIDEO_LOOP=false) camera_id=%s",
                    config.id,
                )
                cap.release()
                cap = None
                video_finished = True
                if is_primary:
                    _clear_latest_frame()
                    set_camera_disconnected()
                stop_event.wait(0.5)
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
                    direction=config.direction,
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
    # Each detect thread blocks on queue.get(); send one sentinel per thread to wake all of them.
    for _ in range(max(1, len(runtime.detect_threads))):
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
    if not wait_until_idle(timeout=90.0):
        logger.warning("Plate OCR still busy during camera reload; waiting for detect threads")
    for dt in runtime.detect_threads:
        dt.join(timeout=90.0)
        if dt.is_alive():
            logger.warning("Detect thread %s did not stop cleanly during camera reload", dt.name)
    with runtime.track_lock:
        for state in runtime.track_states.values():
            state.tracker.reset()
        runtime.track_states.clear()
    _cleanup_track_frames([cfg.id for cfg in runtime.configs])
    _clear_latest_frame()
    set_camera_disconnected()


def _start_runtime(configs: list[CameraConfig]) -> _WorkerRuntime:
    _cleanup_track_frames([cfg.id for cfg in configs])
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

    n_workers = plate_ocr_workers()
    for worker_idx in range(n_workers):
        dt = threading.Thread(
            target=_detect_worker_loop,
            kwargs={"stop_event": runtime.stop_event, "detect_queue": runtime.detect_queue},
            name=f"plate-detect-{worker_idx}",
            daemon=True,
        )
        dt.start()
        runtime.detect_threads.append(dt)

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
                    "id": str(cfg.id),
                    "name": cfg.name,
                    "direction": cfg.direction,
                    "light_profile": cfg.light_profile,
                    "is_primary": cfg.id == runtime.primary_camera_id,
                }
            )
    return {
        "running": runtime is not None and runtime.alive(),
        "camera_count": len(cameras),
        "cameras": cameras,
        "primary_camera_id": str(runtime.primary_camera_id) if runtime and runtime.primary_camera_id else None,
        "ocr_busy": is_busy(),
        "ocr_worker": worker_status(),
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
    ocr = worker_status()
    logger.info(
        "Camera worker reloaded with %s camera(s); OCR worker %s",
        len(configs),
        f"warm pid={ocr['pid']}" if ocr.get("alive") else "will start on first scan",
    )


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
