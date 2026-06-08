"""Lazy-loaded ALPR engine: YOLO detection + PaddleOCR / optional legacy OCR."""

from __future__ import annotations

import logging
import os
import statistics
import threading
from typing import Literal

import numpy as np

from fast_alpr.alpr import ALPR
from fast_alpr.base import BaseOCR, OcrResult
from fast_alpr.default_ocr import DefaultOCR

from helpers.plate_format import clean_plate_ocr_text, rank_ocr_candidate, sanitize_plate_ocr_text
from helpers.plate_ocr_preprocess import build_ocr_variants

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_alpr: ALPR | None = None
_init_error: str | None = None

# Latin + Arabic letters + digits for optional EasyOCR allowlist.
_PLATE_OCR_ALLOWLIST = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "ابتثجحخدذرزسشصضطظعغفقكلمنهوىي"
    "أإئآة"
)


def _use_gpu() -> bool:
    mode = os.environ.get("PLATE_USE_GPU", "auto").strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return False
    if mode in {"1", "true", "yes", "on"}:
        return True
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


def plate_detector_model() -> str:
    return os.environ.get("PLATE_DETECTOR_MODEL", "yolo-v9-s-608-license-plate-end2end").strip()


def plate_detector_confidence() -> float:
    return max(0.05, min(1.0, float(os.environ.get("PLATE_DETECTOR_CONF", "0.25"))))


def plate_ocr_model() -> str:
    return os.environ.get("PLATE_OCR_MODEL", "global-plates-mobile-vit-v2-model").strip()


def plate_ocr_langs() -> list[str]:
    raw = os.environ.get("PLATE_OCR_LANGS", "en").strip()
    langs = [part.strip() for part in raw.split(",") if part.strip()]
    return langs or ["en"]


def paddle_ocr_lang() -> str:
    """PaddleOCR lang code (en, ar, etc.)."""
    raw = os.environ.get("PLATE_PADDLE_LANG", "").strip()
    if raw:
        return raw
    langs = plate_ocr_langs()
    if "ar" in langs or "arabic" in langs:
        return "ar"
    return "en"


def plate_ocr_backend() -> Literal["paddle", "ensemble", "fast", "easyocr"]:
    raw = os.environ.get("PLATE_OCR_BACKEND", "paddle").strip().lower()
    if raw in {"paddle", "paddleocr"}:
        return "paddle"
    if raw in {"fast", "easyocr", "ensemble"}:
        return raw
    return "paddle"


def _ocr_confidence(conf: float | list[float]) -> float:
    if isinstance(conf, list):
        values = [float(c) for c in conf if c and float(c) > 0]
        return statistics.mean(values) if values else 0.0
    return max(0.0, min(1.0, float(conf)))


def _finalize(result: OcrResult | None) -> OcrResult | None:
    if result is None or not result.text:
        return None
    cleaned = clean_plate_ocr_text(result.text)
    if not cleaned:
        return None
    return OcrResult(
        text=cleaned,
        confidence=result.confidence,
        region=result.region,
        region_confidence=result.region_confidence,
    )


def _pick_best(candidates: list[OcrResult]) -> OcrResult | None:
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: rank_ocr_candidate(item.text, _ocr_confidence(item.confidence)),
    )
    return _finalize(best)


def _merge_line_reads(reads: list[tuple[str, float]]) -> OcrResult | None:
    """Join OCR fragments top-to-bottom (multi-line plates)."""
    if len(reads) < 2:
        return None
    merged = sanitize_plate_ocr_text("".join(text for text, _conf in reads))
    if not merged:
        return None
    confs = [float(c) for _text, c in reads if c]
    return OcrResult(
        text=merged,
        confidence=statistics.mean(confs) if confs else 0.0,
    )


class PaddleOcrBackend(BaseOCR):
    """Production OCR via PaddleOCR (PP-OCRv5)."""

    def __init__(self, lang: str, gpu: bool) -> None:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR

        device = "gpu:0" if gpu else "cpu"
        self._ocr = PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            enable_mkldnn=False,
            device=device,
        )
        self._lang = lang

    def _parse_result_page(self, page) -> list[tuple[str, float, float]]:
        """Return (text, confidence, y_center) per detected line."""
        payload = getattr(page, "json", None) or page
        if isinstance(payload, dict) and "res" in payload:
            payload = payload["res"]

        texts = list(payload.get("rec_texts") or [])
        scores = list(payload.get("rec_scores") or [])
        polys = list(payload.get("rec_polys") or payload.get("dt_polys") or [])

        lines: list[tuple[str, float, float]] = []
        for idx, text in enumerate(texts):
            cleaned = str(text or "").strip()
            if not cleaned:
                continue
            conf = float(scores[idx]) if idx < len(scores) else 0.0
            y_center = float(idx)
            if idx < len(polys) and polys[idx]:
                poly = polys[idx]
                y_center = sum(float(p[1]) for p in poly) / max(len(poly), 1)
            lines.append((cleaned, conf, y_center))
        return lines

    def _run_ocr(self, image_bgr: np.ndarray) -> list[tuple[str, float, float]]:
        """Return (text, confidence, y_center) per detected line."""
        if image_bgr is None or image_bgr.size == 0:
            return []

        try:
            pages = self._ocr.predict(image_bgr)
        except Exception:
            logger.debug("PaddleOCR predict failed", exc_info=True)
            return []

        if not pages:
            return []

        lines: list[tuple[str, float, float]] = []
        for page in pages:
            lines.extend(self._parse_result_page(page))
        return lines

    def collect_candidates(self, cropped_plate: np.ndarray) -> list[OcrResult]:
        if cropped_plate is None or cropped_plate.size == 0:
            return []

        results: list[OcrResult] = []
        for img in build_ocr_variants(cropped_plate):
            lines = self._run_ocr(img)
            if not lines:
                continue

            ordered = sorted(lines, key=lambda item: item[2])
            fragment_reads = [(text, conf) for text, conf, _y in ordered]
            merged = _merge_line_reads(fragment_reads)
            if merged is not None:
                results.append(merged)

            for text, conf, _y in ordered:
                cleaned = sanitize_plate_ocr_text(text)
                if cleaned:
                    results.append(OcrResult(text=cleaned, confidence=conf))

            joined = sanitize_plate_ocr_text("".join(text for text, _c, _y in ordered))
            if joined:
                confs = [conf for _text, conf, _y in ordered if conf > 0]
                results.append(
                    OcrResult(
                        text=joined,
                        confidence=statistics.mean(confs) if confs else 0.0,
                    )
                )

        return results

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        return _pick_best(self.collect_candidates(cropped_plate))


class FastPlateOcrBackend(BaseOCR):
    """Plate-trained global OCR (Latin + digits) — legacy fallback."""

    def __init__(self, model: str, gpu: bool) -> None:
        self._ocr = DefaultOCR(hub_ocr_model=model, device="cuda" if gpu else "cpu")

    def collect_candidates(self, cropped_plate: np.ndarray) -> list[OcrResult]:
        if cropped_plate is None or cropped_plate.size == 0:
            return []
        results: list[OcrResult] = []
        for img in build_ocr_variants(cropped_plate):
            out = self._ocr.predict(img)
            if out and out.text:
                cleaned = sanitize_plate_ocr_text(out.text)
                if cleaned:
                    results.append(OcrResult(text=cleaned, confidence=out.confidence))
        return results

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        return _pick_best(self.collect_candidates(cropped_plate))


class EasyOcrBackend(BaseOCR):
    """Legacy fallback OCR with English + Arabic."""

    def __init__(self, langs: list[str], gpu: bool) -> None:
        import easyocr

        self._reader = easyocr.Reader(langs, gpu=gpu, verbose=False)

    def collect_candidates(self, cropped_plate: np.ndarray) -> list[OcrResult]:
        if cropped_plate is None or cropped_plate.size == 0:
            return []
        results: list[OcrResult] = []
        for img in build_ocr_variants(cropped_plate):
            reads = self._reader.readtext(
                img,
                detail=1,
                paragraph=False,
                allowlist=_PLATE_OCR_ALLOWLIST,
            )
            fragment_reads = [(str(text), float(conf)) for _bbox, text, conf in reads]
            merged = _merge_line_reads(fragment_reads)
            if merged is not None:
                results.append(merged)
            for _bbox, text, conf in reads:
                cleaned = sanitize_plate_ocr_text(str(text))
                if cleaned:
                    results.append(OcrResult(text=cleaned, confidence=float(conf)))
        return results

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        return _pick_best(self.collect_candidates(cropped_plate))


class EnsembleOcrBackend(BaseOCR):
    """PaddleOCR + legacy engines; pick highest-confidence read."""

    def __init__(self, model: str, langs: list[str], gpu: bool) -> None:
        self._paddle = PaddleOcrBackend(paddle_ocr_lang(), gpu)
        self._fast = FastPlateOcrBackend(model, gpu)
        self._easy_ocr: EasyOcrBackend | None = None
        self._langs = langs
        self._gpu = gpu

    def _easy_backend(self) -> EasyOcrBackend:
        if self._easy_ocr is None:
            self._easy_ocr = EasyOcrBackend(self._langs, self._gpu)
        return self._easy_ocr

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        candidates: list[OcrResult] = []
        for backend in (self._paddle, self._fast, self._easy_backend()):
            try:
                candidates.extend(backend.collect_candidates(cropped_plate))
            except Exception:
                logger.debug("OCR backend failed", exc_info=True)
                continue
        return _pick_best(candidates)


def _build_ocr_backend(gpu: bool) -> BaseOCR:
    backend = plate_ocr_backend()
    model = plate_ocr_model()
    langs = plate_ocr_langs()

    if backend == "paddle":
        return PaddleOcrBackend(paddle_ocr_lang(), gpu)
    if backend == "fast":
        return FastPlateOcrBackend(model, gpu)
    if backend == "easyocr":
        return EasyOcrBackend(langs, gpu)
    return EnsembleOcrBackend(model, langs, gpu)


def _build_alpr() -> ALPR:
    gpu = _use_gpu()
    detector_model = plate_detector_model()
    detector_conf = plate_detector_confidence()
    providers = _onnx_providers()

    kwargs: dict = {
        "detector_model": detector_model,
        "detector_conf_thresh": detector_conf,
        "ocr": _build_ocr_backend(gpu),
    }
    if providers:
        kwargs["detector_providers"] = providers

    logger.info(
        "Initializing ALPR detector=%s conf=%.2f ocr=%s paddle_lang=%s langs=%s gpu=%s",
        detector_model,
        detector_conf,
        plate_ocr_backend(),
        paddle_ocr_lang(),
        plate_ocr_langs(),
        gpu,
    )
    return ALPR(**kwargs)


def get_alpr() -> ALPR | None:
    global _alpr, _init_error
    if _alpr is not None:
        return _alpr
    if _init_error is not None:
        return None
    with _lock:
        if _alpr is not None:
            return _alpr
        if _init_error is not None:
            return None
        try:
            _alpr = _build_alpr()
            return _alpr
        except Exception as exc:
            _init_error = str(exc)
            logger.exception("Failed to initialize plate ALPR engine")
            return None


def alpr_init_error() -> str | None:
    return _init_error


def ocr_confidence_value(conf: float | list[float]) -> float:
    return _ocr_confidence(conf)
