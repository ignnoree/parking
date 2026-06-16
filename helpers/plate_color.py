"""Classify license plate background color from a plate crop."""

from __future__ import annotations

import cv2
import numpy as np

from helpers.plate_crop import crop_tight_plate

# HSV hue ranges (0-179 in OpenCV) for each chromatic plate color.
_CHROMATIC_RANGES: dict[str, list[tuple[int, int]]] = {
    "red":    [(0, 12), (163, 179)],
    "yellow": [(15, 42)],
    "green":  [(40, 90)],
    "blue":   [(90, 135)],
}


def _sample_plate_hsv(crop_bgr: np.ndarray) -> np.ndarray | None:
    """
    Extract plate-background pixels from the interior of a tight plate crop.

    Steps:
      1. Trim 12% from each horizontal edge and 10% from top/bottom to remove
         car-body paint bleed at the crop border.
      2. Apply a 3×3 median blur to suppress JPEG compression artifacts and
         thin character-stroke edges that bleed into the background.
      3. Convert to HSV and keep only pixels whose Value is in [50, 240]
         (not dark characters/shadows, not blown-out specular reflections).
    """
    h, w = crop_bgr.shape[:2]
    bx = max(1, int(w * 0.12))
    by = max(1, int(h * 0.10))
    roi = crop_bgr[by : max(by + 1, h - by), bx : max(bx + 1, w - bx)]
    if roi.size == 0:
        return None

    blurred = cv2.medianBlur(roi, 3)
    hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)

    v = hsv[:, :, 2]
    mask = (v >= 50) & (v <= 240)
    if not np.any(mask):
        return None

    return hsv[mask]


def classify_plate_background_color(crop_bgr: np.ndarray | None) -> str:
    """
    Estimate plate background color from a tight plate crop.
    Returns: white, red, green, blue, yellow, black, or unknown.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return "unknown"

    h, w = crop_bgr.shape[:2]
    if h < 4 or w < 8:
        return "unknown"

    pixels = _sample_plate_hsv(crop_bgr)
    if pixels is None or len(pixels) < 10:
        return "unknown"

    hue = pixels[:, 0].astype(np.float32)
    sat = pixels[:, 1].astype(np.float32)
    val = pixels[:, 2].astype(np.float32)

    # Black plates: median brightness very low even after dark-pixel exclusion.
    if float(np.median(val)) < 55:
        return "black"

    # Saturation weight: pixels with sat<=30 contribute ~0, sat=255 contributes 1.
    # This means strongly-saturated pixels dominate chromatic scoring, while
    # grey/silver/white pixels are naturally down-weighted.
    sat_w = np.maximum(0.0, sat - 30.0) / 225.0

    scores: dict[str, float] = {}
    for color, ranges in _CHROMATIC_RANGES.items():
        in_range = np.zeros(len(hue), dtype=bool)
        for lo, hi in ranges:
            in_range |= (hue >= lo) & (hue <= hi)
        # Also require minimum saturation so pale tints don't trigger chromatic hits.
        in_range &= sat >= 50
        scores[color] = float(np.sum(sat_w[in_range]))

    total_sat = float(np.sum(sat_w)) + 1e-9
    best_color = max(scores, key=scores.get)
    best_score = scores[best_color] / total_sat

    # Achromatic fraction: pixels with low saturation (white/grey/silver).
    achromatic = float(np.mean(sat < 55))

    # Chromatic color wins when it accounts for ≥30% of saturation-weighted
    # pixels and beats the achromatic mass clearly.
    if best_score >= 0.30 and best_score >= achromatic * 0.55:
        return best_color

    # Fall back to white for low-saturation or bright plates.
    if achromatic >= 0.45 or float(np.median(val)) >= 120:
        return "white"

    return "unknown"


def detect_plate_color_from_frame(frame_bgr: np.ndarray, box: dict | None) -> str:
    """Classify plate background from the tight detector box (no OCR padding)."""
    if frame_bgr is None or frame_bgr.size == 0 or not box:
        return "unknown"
    crop = crop_tight_plate(frame_bgr, box)
    return classify_plate_background_color(crop)
