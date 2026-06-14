"""Classify license plate background color from a plate crop."""

from __future__ import annotations

import cv2
import numpy as np

from helpers.plate_crop import crop_ocr_plate

# GCC / Iran common plate background labels stored in parking log details.
PLATE_COLOR_LABELS = (
    "white",
    "red",
    "green",
    "blue",
    "yellow",
    "black",
    "unknown",
)


def classify_plate_background_color(crop_bgr: np.ndarray | None) -> str:
    """
    Estimate plate background color from border pixels (avoids OCR text in the center).
    Returns one of PLATE_COLOR_LABELS.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return "unknown"

    h, w = crop_bgr.shape[:2]
    if h < 4 or w < 8:
        return "unknown"

    margin = max(1, int(min(h, w) * 0.15))
    strips = [
        crop_bgr[:margin, :],
        crop_bgr[h - margin :, :],
        crop_bgr[:, :margin],
        crop_bgr[:, w - margin :],
    ]
    pixels = np.concatenate(
        [strip.reshape(-1, 3) for strip in strips if strip.size > 0],
        axis=0,
    )
    if pixels.size == 0:
        return "unknown"

    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV).reshape(
        -1, 3
    )
    hue = float(np.median(hsv[:, 0]))
    sat = float(np.median(hsv[:, 1]))
    val = float(np.median(hsv[:, 2]))

    if sat < 45 and val > 155:
        return "white"
    if val < 55:
        return "black"
    if 18 <= hue <= 42 and sat > 75 and val > 95:
        return "yellow"
    if (hue <= 12 or hue >= 168) and sat > 65:
        return "red"
    if 40 <= hue <= 88 and sat > 45:
        return "green"
    if 95 <= hue <= 128 and sat > 45:
        return "blue"
    if sat < 65 and val > 115:
        return "white"
    return "unknown"


def detect_plate_color_from_frame(frame_bgr: np.ndarray, box: dict | None) -> str:
    """Crop the plate region and classify its background color."""
    if frame_bgr is None or frame_bgr.size == 0 or not box:
        return "unknown"
    crop = crop_ocr_plate(frame_bgr, box)
    return classify_plate_background_color(crop)
