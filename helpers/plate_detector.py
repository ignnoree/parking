"""Plate YOLO detector — Ultralytics YOLOv8 (.pt) or ONNX hub (yolo-v9-*)."""

from __future__ import annotations

import logging
import os
import threading

import numpy as np
from open_image_models.detection.core.base import BoundingBox, DetectionResult

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_detector = None
_init_error: str | None = None

_DEFAULT_ULTRALYTICS_PATH = "models/license_plate_detector.pt"
_DEFAULT_ONNX_MODEL = "yolo-v9-s-608-license-plate-end2end"


def _use_gpu() -> bool:
    mode = os.environ.get("PLATE_USE_GPU", "auto").strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return False
    if mode in {"1", "true", "yes", "on"}:
        try:
            import torch

            return torch.cuda.is_available()
        except Exception:
            return False
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def _onnx_providers() -> list[str] | None:
    if not _use_gpu():
        return None
    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        if "CUDAExecutionProvider" in providers:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        logger.debug("Could not configure ONNX GPU providers", exc_info=True)
    return None


def plate_detector_confidence() -> float:
    return max(0.05, min(1.0, float(os.environ.get("PLATE_DETECTOR_CONF", "0.40"))))


def plate_detector_model() -> str:
    """Model path (.pt) or ONNX hub name depending on backend."""
    return os.environ.get("PLATE_DETECTOR_MODEL", _DEFAULT_ULTRALYTICS_PATH).strip()


def plate_onnx_model() -> str:
    return os.environ.get("PLATE_DETECTOR_ONNX_MODEL", _DEFAULT_ONNX_MODEL).strip()


def plate_detector_backend() -> str:
    """
    ultralytics — YOLOv8 .pt (reference repo: models/license_plate_detector.pt)
    onnx — fast-alpr / open-image-models hub (yolo-v9-t-384, etc.)
    auto — .pt file on disk → ultralytics, else onnx
    """
    raw = os.environ.get("PLATE_DETECTOR_BACKEND", "auto").strip().lower()
    if raw in {"ultralytics", "yolo", "yolov8", "yolov8n"}:
        return "ultralytics"
    if raw in {"onnx", "fast_alpr", "open_image_models"}:
        return "onnx"
    model = plate_detector_model()
    if model.endswith(".pt"):
        if os.path.isfile(model) or os.path.isfile(_DEFAULT_ULTRALYTICS_PATH):
            return "ultralytics"
        logger.warning(
            "Ultralytics model %r not found — falling back to ONNX %s",
            model,
            plate_onnx_model(),
        )
        return "onnx"
    if "license-plate-end2end" in model or model.startswith("yolo-v9"):
        return "onnx"
    return "onnx"


class UltralyticsPlateDetector:
    """YOLOv8 plate detector — same stack as computervisioneng ANPR repo."""

    def __init__(self, model_path: str, conf_thresh: float) -> None:
        from ultralytics import YOLO

        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"Ultralytics plate model not found: {model_path!r}. "
                "Place license_plate_detector.pt from the reference repo under models/ "
                f"or set PLATE_DETECTOR_BACKEND=onnx."
            )
        self._model = YOLO(model_path)
        self._conf = conf_thresh
        self._device = "cuda" if _use_gpu() else "cpu"
        self._model_path = model_path
        logger.info(
            "Ultralytics plate detector ready path=%s conf=%.2f device=%s",
            model_path,
            conf_thresh,
            self._device,
        )

    def predict(self, frame: np.ndarray) -> list[DetectionResult]:
        if frame is None or frame.size == 0:
            return []
        output = self._model.predict(
            frame,
            conf=self._conf,
            verbose=False,
            device=self._device,
        )[0]
        detections: list[DetectionResult] = []
        boxes = output.boxes
        if boxes is None:
            return detections
        for row in boxes.data.tolist():
            x1, y1, x2, y2, score, _class_id = row
            x1_i, y1_i = int(max(x1, 0)), int(max(y1, 0))
            x2_i, y2_i = int(min(x2, frame.shape[1])), int(min(y2, frame.shape[0]))
            if x2_i <= x1_i or y2_i <= y1_i:
                continue
            detections.append(
                DetectionResult(
                    label="license_plate",
                    confidence=float(score),
                    bounding_box=BoundingBox(x1=x1_i, y1=y1_i, x2=x2_i, y2=y2_i),
                )
            )
        return detections


def build_plate_detector():
    """Build detector for tracking thread and ALPR child process."""
    backend = plate_detector_backend()
    conf = plate_detector_confidence()

    if backend == "ultralytics":
        model_path = plate_detector_model()
        if not os.path.isfile(model_path) and os.path.isfile(_DEFAULT_ULTRALYTICS_PATH):
            model_path = _DEFAULT_ULTRALYTICS_PATH
        return UltralyticsPlateDetector(model_path, conf)

    from fast_alpr.default_detector import DefaultDetector

    model_name = plate_onnx_model()
    if plate_detector_backend() == "onnx" and plate_detector_model().startswith("yolo-v9"):
        model_name = plate_detector_model()

    kwargs: dict = {"model_name": model_name, "conf_thresh": conf}
    providers = _onnx_providers()
    if providers:
        kwargs["providers"] = providers
    detector = DefaultDetector(**kwargs)
    logger.info(
        "ONNX plate detector ready model=%s conf=%.2f gpu=%s",
        model_name,
        conf,
        _use_gpu(),
    )
    return detector


def _get_detector():
    global _detector, _init_error
    if _detector is not None:
        return _detector
    if _init_error is not None:
        raise RuntimeError(_init_error)
    with _lock:
        if _detector is not None:
            return _detector
        if _init_error is not None:
            raise RuntimeError(_init_error)
        try:
            _detector = build_plate_detector()
            return _detector
        except Exception as exc:
            _init_error = str(exc)
            logger.exception("Failed to initialize plate detector")
            raise


def detector_init_error() -> str | None:
    return _init_error


def detect_plates_in_frame(frame_bgr: np.ndarray) -> list[dict]:
    """
    Run YOLO plate detection on a BGR frame.
    Returns [{box: {x,y,w,h}, confidence}, ...].
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return []

    from helpers.plate_detection_filter import filter_plate_detections

    detector = _get_detector()
    results: list[dict] = []
    for detection in detector.predict(frame_bgr):
        bbox = detection.bounding_box
        x1, y1 = int(max(bbox.x1, 0)), int(max(bbox.y1, 0))
        x2 = int(min(bbox.x2, frame_bgr.shape[1]))
        y2 = int(min(bbox.y2, frame_bgr.shape[0]))
        w, h = max(0, x2 - x1), max(0, y2 - y1)
        if w <= 0 or h <= 0:
            continue
        results.append(
            {
                "box": {"x": x1, "y": y1, "w": w, "h": h},
                "confidence": float(detection.confidence),
            }
        )
    return filter_plate_detections(results, frame_bgr.shape)
