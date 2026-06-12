"""Reject non-plate YOLO boxes (walls, signs, slivers) before OCR."""

from __future__ import annotations

import os


def plate_det_min_width() -> int:
    return max(20, int(os.environ.get("PLATE_DET_MIN_WIDTH", "80")))


def plate_det_min_height() -> int:
    return max(8, int(os.environ.get("PLATE_DET_MIN_HEIGHT", "18")))


def plate_det_min_aspect() -> float:
    """License plates are wide — w/h lower bound."""
    return max(1.2, float(os.environ.get("PLATE_DET_MIN_ASPECT", "2.2")))


def plate_det_max_aspect() -> float:
    return max(3.0, float(os.environ.get("PLATE_DET_MAX_ASPECT", "7.5")))


def plate_det_min_area_ratio() -> float:
    """Min box area as fraction of frame (drop tiny specks)."""
    return max(0.0, float(os.environ.get("PLATE_DET_MIN_AREA_RATIO", "0.00008")))


def filter_plate_detections(
    detections: list[dict],
    frame_shape: tuple[int, ...],
) -> list[dict]:
    if not detections:
        return []

    frame_h = int(frame_shape[0]) if frame_shape else 0
    frame_w = int(frame_shape[1]) if len(frame_shape) > 1 else 0
    frame_area = max(1, frame_h * frame_w)
    min_area = frame_area * plate_det_min_area_ratio()
    min_w = plate_det_min_width()
    min_h = plate_det_min_height()
    min_aspect = plate_det_min_aspect()
    max_aspect = plate_det_max_aspect()

    kept: list[dict] = []
    for det in detections:
        box = det.get("box")
        if not isinstance(box, dict):
            continue
        w = int(box.get("w") or 0)
        h = int(box.get("h") or 0)
        if w < min_w or h < min_h:
            continue
        if w * h < min_area:
            continue
        aspect = w / max(h, 1)
        if aspect < min_aspect or aspect > max_aspect:
            continue
        kept.append(det)
    return kept
