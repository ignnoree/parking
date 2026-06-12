"""Expand plate boxes to vehicle-context crops for snapshots."""

from __future__ import annotations

import os

import cv2
import numpy as np


def _pad_x() -> float:
    return max(0.0, min(2.0, float(os.environ.get("PLATE_CROP_PAD_X", "0.45"))))


def _pad_y_up() -> float:
    return max(0.0, min(6.0, float(os.environ.get("PLATE_CROP_PAD_Y_UP", "2.5"))))


def _pad_y_down() -> float:
    return max(0.0, min(2.0, float(os.environ.get("PLATE_CROP_PAD_Y_DOWN", "0.35"))))


def _ocr_pad_x() -> float:
    """Horizontal padding on each side of the tight detector box for OCR."""
    return max(0.0, min(1.5, float(os.environ.get("PLATE_OCR_BOX_PAD_X", "0.22"))))


def _ocr_pad_y() -> float:
    """Vertical padding on top/bottom of the tight detector box for OCR."""
    return max(0.0, min(1.0, float(os.environ.get("PLATE_OCR_BOX_PAD_Y", "0.15"))))


def expand_ocr_plate_box(
    box: dict | None,
    frame_width: int,
    frame_height: int,
) -> dict | None:
    """Widen the tight YOLO plate box before OCR so edge characters are not clipped."""
    if not box:
        return None

    x = int(box.get("x", 0))
    y = int(box.get("y", 0))
    w = max(1, int(box.get("w", 0)))
    h = max(1, int(box.get("h", 0)))

    pad_x = _ocr_pad_x()
    pad_y = _ocr_pad_y()

    x1 = max(0, int(x - w * pad_x))
    x2 = min(frame_width, int(x + w + w * pad_x))
    y1 = max(0, int(y - h * pad_y))
    y2 = min(frame_height, int(y + h + h * pad_y))

    return {"x": x1, "y": y1, "w": max(1, x2 - x1), "h": max(1, y2 - y1)}


def crop_ocr_plate(frame_bgr: np.ndarray, tight_box: dict | None) -> np.ndarray | None:
    """Crop plate region with OCR padding applied (no snapshot crop expansion)."""
    if frame_bgr is None or frame_bgr.size == 0 or not tight_box:
        return None
    fh, fw = frame_bgr.shape[:2]
    expanded = expand_ocr_plate_box(tight_box, fw, fh)
    if not expanded:
        return None
    x1 = int(expanded["x"])
    y1 = int(expanded["y"])
    x2 = min(fw, x1 + int(expanded["w"]))
    y2 = min(fh, y1 + int(expanded["h"]))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame_bgr[y1:y2, x1:x2].copy()


def expand_plate_box(
    box: dict | None,
    frame_width: int,
    frame_height: int,
) -> dict | None:
    """
    Expand a tight plate box so the crop shows the plate plus surrounding car body.

    Bias: more padding above the plate (trunk/hatch area) and to the sides.
    """
    if not box:
        return None

    x = int(box.get("x", 0))
    y = int(box.get("y", 0))
    w = max(1, int(box.get("w", 0)))
    h = max(1, int(box.get("h", 0)))

    pad_x = _pad_x()
    pad_up = _pad_y_up()
    pad_down = _pad_y_down()

    x1 = max(0, int(x - w * pad_x))
    x2 = min(frame_width, int(x + w + w * pad_x))
    y1 = max(0, int(y - h * pad_up))
    y2 = min(frame_height, int(y + h + h * pad_down))

    crop_w = max(1, x2 - x1)
    crop_h = max(1, y2 - y1)
    return {"x": x1, "y": y1, "w": crop_w, "h": crop_h}


def crop_frame_region(frame_bgr: np.ndarray, box: dict | None) -> np.ndarray | None:
    if frame_bgr is None or frame_bgr.size == 0 or not box:
        return None
    h, w = frame_bgr.shape[:2]
    expanded = expand_plate_box(box, w, h)
    if not expanded:
        return None
    x1 = int(expanded["x"])
    y1 = int(expanded["y"])
    x2 = min(w, x1 + int(expanded["w"]))
    y2 = min(h, y1 + int(expanded["h"]))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame_bgr[y1:y2, x1:x2].copy()


def draw_plate_box_on_crop(crop_bgr: np.ndarray, plate_box: dict, expanded_box: dict) -> np.ndarray:
    """Draw the tight plate rectangle on the expanded crop for review."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr
    out = crop_bgr.copy()
    ex, ey = int(expanded_box["x"]), int(expanded_box["y"])
    px = int(plate_box["x"]) - ex
    py = int(plate_box["y"]) - ey
    pw = int(plate_box["w"])
    ph = int(plate_box["h"])
    cv2.rectangle(out, (px, py), (px + pw, py + ph), (0, 220, 80), 2)
    return out
