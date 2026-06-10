"""MJPEG buffer + plate box overlay for guard live view."""

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
_overlay_boxes: list[dict] = []


def update_overlay_from_plate_results(frame_shape: tuple, result_payload: dict | None) -> None:
    global _overlay_boxes
    if not result_payload or result_payload.get("status") != "ok":
        return
    h, w = int(frame_shape[0]), int(frame_shape[1])
    boxes: list[dict] = []
    for item in result_payload.get("results") or []:
        box = item.get("box") or {}
        if not box:
            continue
        status = item.get("match_status")
        if status == "registered":
            color = "guest" if item.get("is_guest") else "resident"
        else:
            color = "unregistered"
        boxes.append(
            {
                "x": int(box.get("x", 0)),
                "y": int(box.get("y", 0)),
                "w": int(box.get("w", 0)),
                "h": int(box.get("h", 0)),
                "color": color,
                "label": item.get("plate_text") or item.get("plate_normalized") or "",
            }
        )
    with _lock:
        _overlay_boxes = boxes


def _draw_boxes(frame: np.ndarray) -> np.ndarray:
    out = frame.copy()
    colors = {
        "resident": (40, 200, 80),
        "guest": (0, 220, 255),
        "unregistered": (60, 60, 240),
    }
    with _lock:
        boxes = list(_overlay_boxes)
    for b in boxes:
        c = colors.get(b.get("color"), (200, 200, 200))
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        cv2.rectangle(out, (x, y), (x + w, y + h), c, 2)
        label = str(b.get("label") or "")
        if label:
            cv2.putText(out, label, (x, max(0, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 1)
    return out


def publish_frame(frame: np.ndarray) -> None:
    global _latest_jpeg, _camera_connected, _frame_sequence
    max_w = int(os.environ.get("LIVE_STREAM_MAX_WIDTH", "640") or "640")
    h, w = frame.shape[:2]
    if w > max_w:
        scale = max_w / w
        frame = cv2.resize(frame, (max_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    annotated = _draw_boxes(frame)
    q = int(os.environ.get("LIVE_STREAM_JPEG_QUALITY", "70"))
    ok, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), q])
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
    global _latest_jpeg, _camera_connected, _frame_sequence, _overlay_boxes
    with _frame_ready:
        _latest_jpeg = None
        _camera_connected = False
        _overlay_boxes = []
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
            "plates_in_overlay": len(_overlay_boxes),
            "frame_sequence": _frame_sequence,
        }
