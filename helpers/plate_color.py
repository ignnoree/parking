"""Classify license plate background color from a plate crop."""

from __future__ import annotations

import cv2
import numpy as np

from helpers.plate_crop import crop_tight_plate

_CHROMATIC = ("blue", "red", "green", "yellow")


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


def _color_fractions(hsv: np.ndarray) -> dict[str, float]:
    """Share of background pixels in each color bucket."""
    if hsv.size == 0:
        return {}
    h = hsv[:, 0].astype(np.float32)
    s = hsv[:, 1].astype(np.float32)
    v = hsv[:, 2].astype(np.float32)
    return {
        "white": float(np.mean((s < 72) & (v > 65))),
        "bright_white": float(np.mean((s < 60) & (v > 95))),
        "low_sat": float(np.mean(s < 72)),
        "blue": float(np.mean((h >= 95) & (h <= 130) & (s >= 85) & (v >= 70))),
        "red": float(np.mean(((h <= 10) | (h >= 170)) & (s >= 85) & (v >= 70))),
        "green": float(np.mean((h >= 45) & (h <= 85) & (s >= 80) & (v >= 70))),
        "yellow": float(np.mean((h >= 18) & (h <= 42) & (s >= 85) & (v >= 80))),
        "black": float(np.mean(v < 45)),
    }


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
    fr = _color_fractions(hsv)
    if not fr:
        return "unknown"

    if fr["black"] > 0.5:
        return "black"

    chroma = {name: fr[name] for name in _CHROMATIC}
    best_chroma = max(chroma, key=chroma.get)
    best_frac = chroma[best_chroma]

    # Chromatic laminate must clearly dominate (avoids blue car paint bleeding in).
    if best_frac >= 0.52 and best_frac > fr["white"] + 0.12:
        return best_chroma

    if fr["white"] >= 0.38 or fr["bright_white"] >= 0.25 or fr["low_sat"] >= 0.62:
        return "white"

    if fr["white"] >= 0.25 and best_frac < 0.35:
        return "white"

    return "unknown"


def detect_plate_color_from_frame(frame_bgr: np.ndarray, box: dict | None) -> str:
    """Classify plate background from the tight detector box (no OCR padding)."""
    if frame_bgr is None or frame_bgr.size == 0 or not box:
        return "unknown"
    crop = crop_tight_plate(frame_bgr, box)
    return classify_plate_background_color(crop)
