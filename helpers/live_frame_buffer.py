"""MJPEG buffer for the live camera preview."""

from __future__ import annotations

import logging
import os
import threading
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_frame_ready = threading.Condition(_lock)
_latest_jpeg: bytes | None = None
_camera_connected: bool = False
_frame_sequence: int = 0


def publish_frame(frame: np.ndarray) -> None:
    global _latest_jpeg, _camera_connected, _frame_sequence
    max_w = int(os.environ.get("LIVE_STREAM_MAX_WIDTH", "640") or "640")
    h, w = frame.shape[:2]
    scale = 1.0
    if w > max_w:
        scale = max_w / w
        frame = cv2.resize(frame, (max_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    q = int(os.environ.get("LIVE_STREAM_JPEG_QUALITY", "70"))
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    if not ok:
        logger.warning("Failed to encode live stream JPEG frame")
        return
    data = buf.tobytes()
    with _frame_ready:
        _latest_jpeg = data
        _camera_connected = True
        _frame_sequence += 1
        _frame_ready.notify_all()


def clear_stream_buffer() -> None:
    """Drop the last JPEG so MJPEG clients do not keep showing a previous camera."""
    global _latest_jpeg, _camera_connected, _frame_sequence
    with _frame_ready:
        _latest_jpeg = None
        _camera_connected = False
        _frame_sequence += 1
        _frame_ready.notify_all()


def set_camera_disconnected() -> None:
    clear_stream_buffer()


def wait_for_new_jpeg(after_sequence: int, timeout: float) -> tuple[bytes | None, int]:
    deadline = time.monotonic() + timeout
    with _frame_ready:
        while _frame_sequence <= after_sequence:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (_latest_jpeg if _camera_connected else None), _frame_sequence
            _frame_ready.wait(timeout=remaining)
        return (_latest_jpeg if _camera_connected else None), _frame_sequence


def get_frame_sequence() -> int:
    with _lock:
        return _frame_sequence


def get_stream_status() -> dict:
    with _lock:
        return {
            "connected": _camera_connected,
            "has_frame": _latest_jpeg is not None,
            "frame_sequence": _frame_sequence,
        }
