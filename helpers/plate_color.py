"""Classify license plate background color from a plate crop.

Production approach: K-means (k=2) in LAB color space.
  - LAB separates luminance (L) from chrominance (a*, b*) so near-white plates
    with slight lighting tints never score as chromatic colors.
  - K-means finds the two dominant pixel clusters (background vs. characters)
    without any threshold tuning; the larger cluster is always the background
    because plate backgrounds have more area than character strokes.
  - The background cluster centroid is classified using signed a*/b* thresholds
    on the perceptually-uniform axes: +a*=red, -a*=green, +b*=yellow, -b*=blue.
"""

from __future__ import annotations

import cv2
import numpy as np

from helpers.plate_crop import crop_tight_plate

# K-means termination: stop after 10 iterations or when centres move < 1 LAB unit.
_KMEANS_CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)


def _dominant_background_lab(roi_bgr: np.ndarray) -> np.ndarray | None:
    """
    Split the crop into two K-means clusters in LAB space and return the pixels
    of the background cluster.

    Background = the larger cluster.  Plate backgrounds always occupy more area
    than character strokes, making pixel count a robust separator that needs no
    threshold tuning and works on all plate types.

    Returns an Nx3 float32 array (LAB pixels) or None if the crop is too small.
    """
    lab = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2LAB)
    flat = lab.reshape(-1, 3).astype(np.float32)
    if len(flat) < 20:
        return None

    _, labels, _ = cv2.kmeans(flat, 2, None, _KMEANS_CRITERIA, 3, cv2.KMEANS_PP_CENTERS)
    labels = labels.flatten()

    count0 = int(np.sum(labels == 0))
    count1 = int(np.sum(labels == 1))
    bg_label = 0 if count0 >= count1 else 1

    bg_pixels = flat[labels == bg_label]
    return bg_pixels if len(bg_pixels) >= 5 else None


def _classify_lab(bg_pixels: np.ndarray) -> str:
    """
    Map background LAB pixels to a plate color name.

    OpenCV 8-bit LAB encoding:
      L  ∈ [0, 255]   (0=black, 255=white)
      a  ∈ [0, 255]   (128=neutral → signed a* = a - 128: +red, -green)
      b  ∈ [0, 255]   (128=neutral → signed b* = b - 128: +yellow, -blue)

    Chroma = sqrt(a*² + b*²).  Near-white / grey plates have chroma < ~14
    regardless of any slight colour cast from lighting or JPEG compression,
    so they never fall into the chromatic branches.

    Chromatic thresholds are set conservatively: we prefer "white" on ambiguous
    cases because white is far more common than any chromatic plate type.
    """
    L   = float(np.median(bg_pixels[:, 0]))
    a_s = float(np.median(bg_pixels[:, 1])) - 128.0   # signed a*
    b_s = float(np.median(bg_pixels[:, 2])) - 128.0   # signed b*
    chroma = float(np.sqrt(a_s ** 2 + b_s ** 2))

    # ── Black ─────────────────────────────────────────────────────────────────
    if L < 65:
        return "black"

    # ── Achromatic (white / grey / silver) ────────────────────────────────────
    # Chroma < 14 catches all near-white plates regardless of colour-temperature
    # tint; this threshold is the single most important guard against white→blue.
    if chroma < 14:
        return "white"

    # ── Chromatic — require a decisive signal on the relevant axis ────────────
    # Blue:   strong -b*, |b*| must dominate |a*| to reject blue-grey borders
    if b_s < -13 and abs(b_s) > abs(a_s) * 1.2:
        return "blue"

    # Green:  strong -a*, and -a* must clearly dominate +b* so that
    #         yellow-green hues don't fall through to yellow instead.
    if a_s < -20 and abs(a_s) > abs(b_s) * 0.7:
        return "green"

    # Yellow: strong +b*, a* not in strongly-negative green territory
    if b_s > 22 and b_s > abs(a_s) * 0.9 and a_s > -20:
        return "yellow"

    # Red:    strong +a*, b* not going negative (not blue-red mix)
    if a_s > 22 and b_s > -10:
        return "red"

    # Weak chroma that didn't reach any strong chromatic threshold → white.
    return "white"


def classify_plate_background_color(crop_bgr: np.ndarray | None) -> str:
    """
    Estimate plate background color from a tight plate crop.

    Returns one of: white, red, green, blue, yellow, black, unknown.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return "unknown"

    h, w = crop_bgr.shape[:2]
    if h < 6 or w < 12:
        return "unknown"

    # Trim 12 % on left/right, 10 % top/bottom to remove car-body paint at the
    # edge of the YOLO detector box.
    bx = max(1, int(w * 0.12))
    by = max(1, int(h * 0.10))
    roi = crop_bgr[by: max(by + 1, h - by), bx: max(bx + 1, w - bx)]
    if roi.size == 0:
        return "unknown"

    # Light Gaussian blur to suppress JPEG block artefacts before K-means.
    roi = cv2.GaussianBlur(roi, (3, 3), 0)

    bg = _dominant_background_lab(roi)
    if bg is None:
        return "unknown"

    return _classify_lab(bg)


def detect_plate_color_from_frame(frame_bgr: np.ndarray, box: dict | None) -> str:
    """Classify plate background from the tight detector box (no OCR padding)."""
    if frame_bgr is None or frame_bgr.size == 0 or not box:
        return "unknown"
    crop = crop_tight_plate(frame_bgr, box)
    return classify_plate_background_color(crop)
