"""Preprocess plate crops before OCR: perspective, deblur, contrast, night."""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager

import cv2
import numpy as np

_light_profile_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("light_profile", default="normal")


@contextmanager
def light_profile_scope(profile: str):
    token = _light_profile_ctx.set((profile or "normal").strip().lower())
    try:
        yield
    finally:
        _light_profile_ctx.reset(token)


def _active_light_profile() -> str:
    return _light_profile_ctx.get()


def _flag(name: str, *, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _upscale_factor() -> float:
    return max(1.0, min(4.0, float(os.environ.get("PLATE_OCR_UPSCALE", "2.0"))))


def _night_brightness_threshold() -> float:
    return max(10.0, min(200.0, float(os.environ.get("PLATE_NIGHT_BRIGHTNESS_THRESHOLD", "85"))))


def _order_quad_points(points: np.ndarray) -> np.ndarray:
    """Order 4 points: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = points.sum(axis=1)
    rect[0] = points[np.argmin(s)]
    rect[2] = points[np.argmax(s)]
    diff = np.diff(points, axis=1)
    rect[1] = points[np.argmin(diff)]
    rect[3] = points[np.argmax(diff)]
    return rect


def correct_perspective(crop_bgr: np.ndarray) -> np.ndarray:
    """Warp skewed plate to a flat rectangle when a quadrilateral is found."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr
    if not _flag("PLATE_PREPROCESS_PERSPECTIVE"):
        return crop_bgr

    h, w = crop_bgr.shape[:2]
    if h < 12 or w < 40:
        return crop_bgr

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 40, 140)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_area = h * w * 0.12
    quad: np.ndarray | None = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(approx) >= min_area:
            quad = approx.reshape(4, 2).astype(np.float32)
            break

    if quad is None:
        return crop_bgr

    ordered = _order_quad_points(quad)
    width = int(max(np.linalg.norm(ordered[1] - ordered[0]), np.linalg.norm(ordered[2] - ordered[3])))
    height = int(max(np.linalg.norm(ordered[3] - ordered[0]), np.linalg.norm(ordered[2] - ordered[1])))
    width = max(width, w)
    height = max(height, h)

    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, dst)
    return cv2.warpPerspective(crop_bgr, matrix, (width, height), flags=cv2.INTER_CUBIC)


def deblur_plate(crop_bgr: np.ndarray) -> np.ndarray:
    """Reduce blur/noise while keeping character edges."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr
    if not _flag("PLATE_PREPROCESS_DEBLUR"):
        return crop_bgr

    denoised = cv2.bilateralFilter(crop_bgr, d=5, sigmaColor=60, sigmaSpace=60)
    gray = cv2.cvtColor(denoised, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.6)
    sharp = cv2.addWeighted(gray, 1.55, blur, -0.55, 0)
    return cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)


def _mean_brightness(gray: np.ndarray) -> float:
    return float(np.mean(gray))


def is_night_crop(crop_bgr: np.ndarray) -> bool:
    if crop_bgr is None or crop_bgr.size == 0:
        return False
    profile = _active_light_profile()
    if profile == "low_light":
        return True
    if profile == "high_glare":
        return False
    if not _flag("PLATE_PREPROCESS_NIGHT_AUTO"):
        return False
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    return _mean_brightness(gray) < _night_brightness_threshold()


def enhance_night(crop_bgr: np.ndarray) -> np.ndarray:
    """Lift shadows and local contrast for low-light plate crops."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    brightness = _mean_brightness(gray)
    gamma = 1.9 if brightness < 55 else 1.45 if brightness < _night_brightness_threshold() else 1.15
    inv_gamma = 1.0 / gamma
    table = np.array([((i / 255.0) ** inv_gamma) * 255 for i in range(256)]).astype("uint8")
    lifted = cv2.LUT(gray, table)

    clahe = cv2.createCLAHE(clipLimit=3.8, tileGridSize=(8, 8))
    enhanced = clahe.apply(lifted)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def enhance_contrast(crop_bgr: np.ndarray, *, night: bool = False) -> np.ndarray:
    """CLAHE + optional upscale for OCR-friendly contrast."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr
    if not _flag("PLATE_PREPROCESS_CONTRAST", default="true"):
        return crop_bgr

    if night:
        crop_bgr = enhance_night(crop_bgr)

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    profile = _active_light_profile()
    if profile == "high_glare":
        clip = 4.5 if night else 3.5
    elif profile == "low_light":
        clip = 4.0 if night else 3.0
    else:
        clip = 3.5 if night else 2.5
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    scale = _upscale_factor()
    if scale > 1.0:
        enhanced = cv2.resize(
            enhanced,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_CUBIC,
        )

    blur = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=0.9)
    sharpened = cv2.addWeighted(enhanced, 1.35, blur, -0.35, 0)
    return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)


def suppress_divider_lines(crop_bgr: np.ndarray) -> np.ndarray:
    """Mask thin horizontal/vertical divider lines that OCR reads as '=' or '|'."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = binary.shape[:2]

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, w // 4), 1))
    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=1)

    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(8, h // 3)))
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel, iterations=1)

    mask = cv2.bitwise_or(h_lines, v_lines)
    cleaned = gray.copy()
    cleaned[mask > 0] = 255
    return cv2.cvtColor(cleaned, cv2.COLOR_GRAY2BGR)


def preprocess_plate_crop(crop_bgr: np.ndarray) -> np.ndarray:
    """Full pipeline: perspective -> deblur -> contrast (auto night)."""
    if crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr

    night = is_night_crop(crop_bgr)
    img = correct_perspective(crop_bgr)
    img = suppress_divider_lines(img)
    img = deblur_plate(img)
    return enhance_contrast(img, night=night)


def build_ocr_variants(crop_bgr: np.ndarray) -> list[np.ndarray]:
    """Return preprocessed crops for OCR engines to try (deduped by shape)."""
    if crop_bgr is None or crop_bgr.size == 0:
        return []

    night = is_night_crop(crop_bgr)
    flat = correct_perspective(crop_bgr)
    cleaned = suppress_divider_lines(flat)

    variants: list[np.ndarray] = [
        crop_bgr,
        flat,
        preprocess_plate_crop(crop_bgr),
        enhance_contrast(deblur_plate(cleaned), night=False),
    ]
    if night:
        variants.append(enhance_contrast(deblur_plate(cleaned), night=True))

    seen: set[tuple[int, int, int]] = set()
    unique: list[np.ndarray] = []
    for img in variants:
        if img is None or img.size == 0:
            continue
        key = (img.shape[0], img.shape[1], img.shape[2] if img.ndim == 3 else 1)
        if key in seen:
            continue
        seen.add(key)
        unique.append(img)
    return unique or [crop_bgr]
