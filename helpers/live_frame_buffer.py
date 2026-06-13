"""MJPEG buffer + timed plate box overlay when a parking log is created."""

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
_logged_flashes: list[dict] = []


def live_log_overlay_seconds() -> float:
    return max(0.5, min(10.0, float(os.environ.get("LIVE_LOG_OVERLAY_SECONDS", "2"))))


def _prune_expired_flashes(now: float | None = None) -> None:
    global _logged_flashes
    cutoff = now if now is not None else time.monotonic()
    _logged_flashes = [f for f in _logged_flashes if float(f.get("expires_at") or 0) > cutoff]


def flash_logged_plates(plates: list[dict]) -> None:
    """Show a bounding box + plate label on the live stream for a few seconds after logging."""
    if not plates:
        return
    expires_at = time.monotonic() + live_log_overlay_seconds()
    new_flashes: list[dict] = []
    for item in plates:
        if not isinstance(item, dict):
            continue
        box = item.get("box")
        if not isinstance(box, dict):
            continue
        w = int(box.get("w") or 0)
        h = int(box.get("h") or 0)
        if w <= 0 or h <= 0:
            continue
        status = item.get("match_status") or "unregistered"
        if status == "registered":
            color = "guest" if item.get("is_guest") else "resident"
        elif status == "uncertain":
            color = "uncertain"
        else:
            color = "unregistered"
        new_flashes.append(
            {
                "x": int(box.get("x") or 0),
                "y": int(box.get("y") or 0),
                "w": w,
                "h": h,
                "color": color,
                "label": str(item.get("plate_text") or item.get("plate_normalized") or ""),
                "expires_at": expires_at,
            }
        )
    if not new_flashes:
        return
    with _lock:
        _prune_expired_flashes()
        _logged_flashes.extend(new_flashes)


def update_overlay_from_plate_results(frame_shape: tuple, result_payload: dict | None) -> None:
    """Backward-compatible: flash only plates that were actually logged."""
    if not result_payload:
        return
    logged = result_payload.get("logged_plates")
    if isinstance(logged, list) and logged:
        flash_logged_plates(logged)


def _draw_flashes(frame: np.ndarray, *, scale: float = 1.0) -> np.ndarray:
    out = frame.copy()
    colors = {
        "resident": (40, 200, 80),
        "guest": (0, 220, 255),
        "unregistered": (60, 60, 240),
        "uncertain": (0, 200, 255),
    }
    now = time.monotonic()
    with _lock:
        _prune_expired_flashes(now)
        flashes = list(_logged_flashes)
    for b in flashes:
        c = colors.get(b.get("color"), (200, 200, 200))
        x = int(b["x"] * scale)
        y = int(b["y"] * scale)
        w = max(1, int(b["w"] * scale))
        h = max(1, int(b["h"] * scale))
        cv2.rectangle(out, (x, y), (x + w, y + h), c, 3)
        label = str(b.get("label") or "")
        if not label:
            continue
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.65
        thickness = 2
        (tw, th), baseline = cv2.getTextSize(label, font, scale, thickness)
        ty = max(th + 8, y - 8)
        cv2.rectangle(out, (x, ty - th - 6), (x + tw + 8, ty + baseline + 2), c, -1)
        cv2.putText(out, label, (x + 4, ty), font, scale, (255, 255, 255), thickness)
    return out


def publish_frame(frame: np.ndarray) -> None:
    global _latest_jpeg, _camera_connected, _frame_sequence
    max_w = int(os.environ.get("LIVE_STREAM_MAX_WIDTH", "640") or "640")
    h, w = frame.shape[:2]
    scale = 1.0
    if w > max_w:
        scale = max_w / w
        frame = cv2.resize(frame, (max_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    annotated = _draw_flashes(frame, scale=scale)
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
    global _latest_jpeg, _camera_connected, _frame_sequence, _logged_flashes
    with _frame_ready:
        _latest_jpeg = None
        _camera_connected = False
        _logged_flashes = []
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
        _prune_expired_flashes()
        return {
            "connected": _camera_connected,
            "has_frame": _latest_jpeg is not None,
            "logged_flashes": len(_logged_flashes),
            "frame_sequence": _frame_sequence,
        }
