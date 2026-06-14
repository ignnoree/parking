"""Classify license plate background color from a plate crop."""

from __future__ import annotations

import cv2
import numpy as np

from helpers.plate_crop import crop_tight_plate


def _plate_background_pixels(crop_bgr: np.ndarray) -> np.ndarray:
    """
    Sample pixels from the plate laminate only (top/bottom bands, center width).
    Avoids outer edges (car paint) and dark OCR characters.
    """
    h, w = crop_bgr.shape[:2]
    band_h = max(1, int(h * 0.22))
    x1 = int(w * 0.18)
    x2 = max(x1 + 1, int(w * 0.82))

    strips = [
        crop_bgr[:band_h, x1:x2],
        crop_bgr[h - band_h :, x1:x2],
    ]
    pixels = np.concatenate(
        [strip.reshape(-1, 3) for strip in strips if strip.size > 0],
        axis=0,
    )
    if pixels.size == 0:
        return pixels

    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV).reshape(
        -1, 3
    )
    # Drop black/dark OCR glyphs; keep plate background laminate.
    keep = hsv[:, 2] >= 60
    return pixels[keep] if np.any(keep) else pixels


def _is_mostly_white_laminate(hsv: np.ndarray) -> bool:
    """True when a clear majority of background pixels are bright and low-saturation."""
    if hsv.size == 0:
        return False
    sat = hsv[:, 1].astype(np.float32)
    val = hsv[:, 2].astype(np.float32)
    white_like = (sat < 72) & (val > 100)
    return float(np.mean(white_like)) >= 0.42


def classify_plate_background_color(crop_bgr: np.ndarray | None) -> str:
    """
    Estimate plate background color from tight plate crop (not padded car body).
    Returns white, red, green, blue, yellow, black, or unknown.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return "unknown"

    h, w = crop_bgr.shape[:2]
    if h < 4 or w < 8:
        return "unknown"

    pixels = _plate_background_pixels(crop_bgr)
    if pixels.size == 0:
        return "unknown"

    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV).reshape(
        -1, 3
    )

    if _is_mostly_white_laminate(hsv):
        return "white"

    hue = float(np.median(hsv[:, 0]))
    sat = float(np.median(hsv[:, 1]))
    val = float(np.median(hsv[:, 2]))

    # Neutral / GCC standard white plates (also cool-lit off-white).
    if sat < 72 and val > 105:
        return "white"
    if val < 50:
        return "black"

    # Chromatic plates need clearly saturated laminate (not white with color cast).
    if sat < 72:
        return "white" if val > 90 else "unknown"

    if 18 <= hue <= 42 and sat > 80 and val > 90:
        return "yellow"
    if (hue <= 10 or hue >= 170) and sat > 80:
        return "red"
    if 45 <= hue <= 85 and sat > 72:
        return "green"
    if 95 <= hue <= 130 and sat > 72:
        return "blue"

    if sat < 85 and val > 95:
        return "white"
    return "unknown"


def detect_plate_color_from_frame(frame_bgr: np.ndarray, box: dict | None) -> str:
    """Classify plate background from the tight detector box (no OCR padding)."""
    if frame_bgr is None or frame_bgr.size == 0 or not box:
        return "unknown"
    crop = crop_tight_plate(frame_bgr, box)
    return classify_plate_background_color(crop)
