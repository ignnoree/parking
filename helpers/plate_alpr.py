"""Lazy-loaded ALPR engine: YOLO detection + PaddleOCR / optional legacy OCR."""

from __future__ import annotations

import logging
import os
import statistics
import threading
from typing import Literal

import cv2
import numpy as np

from fast_alpr.alpr import ALPR, ALPRResult
from fast_alpr.base import BaseOCR, OcrResult
from fast_alpr.default_ocr import DefaultOCR

from helpers.plate_crop import crop_ocr_plate
from helpers.plate_format import (
    clean_plate_ocr_text,
    is_plausible_plate,
    ocr_read_variants,
    rank_ocr_candidate,
    sanitize_plate_ocr_text,
)
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


def plate_detector_model() -> str:
    return os.environ.get("PLATE_DETECTOR_MODEL", "yolo-v9-s-608-license-plate-end2end").strip()


def plate_detector_confidence() -> float:
    return max(0.05, min(1.0, float(os.environ.get("PLATE_DETECTOR_CONF", "0.25"))))


def plate_ocr_model() -> str:
    return os.environ.get("PLATE_OCR_MODEL", "global-plates-mobile-vit-v2-model").strip()


def plate_ocr_langs() -> list[str]:
    raw = os.environ.get("PLATE_OCR_LANGS", "en,ar").strip()
    langs = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return langs or ["en", "ar"]


def paddle_ocr_langs() -> list[str]:
    """PaddleOCR lang codes to run (e.g. en + ar for Latin and Arabic plates)."""
    raw = os.environ.get("PLATE_PADDLE_LANGS", "").strip()
    if raw:
        return [part.strip().lower() for part in raw.split(",") if part.strip()]
    raw_single = os.environ.get("PLATE_PADDLE_LANG", "").strip().lower()
    if raw_single:
        return [raw_single]
    codes: list[str] = []
    for token in plate_ocr_langs():
        if token in {"ar", "arabic"}:
            codes.append("ar")
        elif token in {"en", "english", "latin"}:
            codes.append("en")
    return codes or ["en", "ar"]


def paddle_ocr_lang() -> str:
    """Primary Paddle lang (logging); use paddle_ocr_langs() for bilingual reads."""
    langs = paddle_ocr_langs()
    return "+".join(langs)


def easyocr_langs() -> list[str]:
    """EasyOCR language codes derived from PLATE_OCR_LANGS."""
    codes: list[str] = []
    for token in plate_ocr_langs():
        if token in {"ar", "arabic"}:
            codes.append("ar")
        elif token in {"en", "english", "latin"}:
            codes.append("en")
    return codes or ["en", "ar"]


def plate_ocr_backend() -> Literal["paddle", "bilingual", "ensemble", "fast", "easyocr", "tiered"]:
    raw = os.environ.get("PLATE_OCR_BACKEND", "bilingual").strip().lower()
    if raw in {"paddle", "paddleocr"}:
        return "paddle"
    if raw in {"bilingual", "dual", "both"}:
        return "bilingual"
    if raw == "fast":
        return "fast"
    if raw == "easyocr":
        return "easyocr"
    if raw == "ensemble":
        return "ensemble"
    if raw in {"tiered", "tier", "fast+bilingual"}:
        return "tiered"
    return "bilingual"


def plate_ocr_early_exit_confidence() -> float:
    """One plausible read at/above this confidence stops further OCR passes (0 disables)."""
    return max(0.0, min(1.0, float(os.environ.get("PLATE_OCR_EARLY_EXIT_CONF", "0.75"))))


def _early_exit_hit(candidates: list[OcrResult]) -> bool:
    """True when a candidate is plausible and confident enough to skip remaining OCR passes."""
    threshold = plate_ocr_early_exit_confidence()
    if threshold <= 0:
        return False
    for cand in candidates:
        if cand is None or not cand.text:
            continue
        if _ocr_confidence(cand.confidence) < threshold:
            continue
        if is_plausible_plate(cand.text):
            return True
    return False


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
    finalized = [_finalize(c) for c in candidates]
    valid = [c for c in finalized if c is not None and c.text]
    if not valid:
        return None
    return max(
        valid,
        key=lambda item: rank_ocr_candidate(item.text, _ocr_confidence(item.confidence)),
    )


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


def _with_ambiguity_variants(results: list[OcrResult]) -> list[OcrResult]:
    expanded = list(results)
    for result in results:
        for variant in ocr_read_variants(result.text):
            if variant and variant != result.text:
                expanded.append(
                    OcrResult(
                        text=variant,
                        confidence=max(0.0, _ocr_confidence(result.confidence) * 0.99),
                    )
                )
    return expanded


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

            if _early_exit_hit(results):
                break

        return _with_ambiguity_variants(results)

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        return _pick_best(self.collect_candidates(cropped_plate))


class BilingualPaddleOcrBackend(BaseOCR):
    """Run PaddleOCR once per language (e.g. en + ar) and pick the best plate read."""

    def __init__(self, langs: list[str], gpu: bool) -> None:
        unique = list(dict.fromkeys(lang for lang in langs if lang))
        self._langs = unique or ["en", "ar"]
        self._backends = [PaddleOcrBackend(lang, gpu) for lang in self._langs]

    def collect_candidates(self, cropped_plate: np.ndarray) -> list[OcrResult]:
        if cropped_plate is None or cropped_plate.size == 0:
            return []
        candidates: list[OcrResult] = []
        for backend in self._backends:
            try:
                candidates.extend(backend.collect_candidates(cropped_plate))
            except Exception:
                logger.debug("PaddleOCR lang=%s failed", backend._lang, exc_info=True)
            if _early_exit_hit(candidates):
                break
        return candidates

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
                    results.append(
                        OcrResult(text=cleaned, confidence=_ocr_confidence(out.confidence))
                    )
            if _early_exit_hit(results):
                break
        return _with_ambiguity_variants(results)

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
        return _with_ambiguity_variants(results)

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        return _pick_best(self.collect_candidates(cropped_plate))


class EnsembleOcrBackend(BaseOCR):
    """Bilingual Paddle + global plate model + EasyOCR; pick highest-confidence read."""

    def __init__(self, model: str, gpu: bool) -> None:
        self._paddle = BilingualPaddleOcrBackend(paddle_ocr_langs(), gpu)
        self._fast = FastPlateOcrBackend(model, gpu)
        self._easy_ocr: EasyOcrBackend | None = None
        self._gpu = gpu

    def _easy_backend(self) -> EasyOcrBackend:
        if self._easy_ocr is None:
            self._easy_ocr = EasyOcrBackend(easyocr_langs(), self._gpu)
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


class TieredOcrBackend(BaseOCR):
    """
    Plate-trained fast model (CCT) first; bilingual Paddle (en+ar) only when the
    fast tier has no confident plausible read. Keeps Latin plates fast and
    accurate while still reading Arabic-script plates.
    """

    def __init__(self, model: str, gpu: bool) -> None:
        self._fast = FastPlateOcrBackend(model, gpu)
        self._paddle = BilingualPaddleOcrBackend(paddle_ocr_langs(), gpu)

    def collect_candidates(self, cropped_plate: np.ndarray) -> list[OcrResult]:
        if cropped_plate is None or cropped_plate.size == 0:
            return []
        candidates: list[OcrResult] = []
        try:
            candidates.extend(self._fast.collect_candidates(cropped_plate))
        except Exception:
            logger.debug("Fast OCR tier failed", exc_info=True)
        if _early_exit_hit(candidates):
            return candidates
        try:
            candidates.extend(self._paddle.collect_candidates(cropped_plate))
        except Exception:
            logger.debug("Paddle OCR tier failed", exc_info=True)
        return candidates

    def predict(self, cropped_plate: np.ndarray) -> OcrResult | None:
        return _pick_best(self.collect_candidates(cropped_plate))


def _build_paddle_backend(gpu: bool) -> BaseOCR:
    langs = paddle_ocr_langs()
    if len(langs) <= 1:
        return PaddleOcrBackend(langs[0] if langs else "en", gpu)
    return BilingualPaddleOcrBackend(langs, gpu)


def _build_ocr_backend(gpu: bool) -> BaseOCR:
    backend = plate_ocr_backend()
    model = plate_ocr_model()

    if backend == "paddle":
        return _build_paddle_backend(gpu)
    if backend == "bilingual":
        return BilingualPaddleOcrBackend(paddle_ocr_langs(), gpu)
    if backend == "fast":
        return FastPlateOcrBackend(model, gpu)
    if backend == "easyocr":
        return EasyOcrBackend(easyocr_langs(), gpu)
    if backend == "tiered":
        return TieredOcrBackend(model, gpu)
    return EnsembleOcrBackend(model, gpu)


class PaddedALPR(ALPR):
    """Run OCR on a padded plate crop so leading/trailing characters are not clipped."""

    def predict(self, frame: np.ndarray | str) -> list[ALPRResult]:
        if isinstance(frame, str):
            img_path = frame
            img = cv2.imread(img_path)
            if img is None:
                raise ValueError(f"Failed to load image from path: {img_path}")
        else:
            img = frame

        plate_detections = self.detector.predict(img)
        alpr_results: list[ALPRResult] = []
        for detection in plate_detections:
            bbox = detection.bounding_box
            tight_box = {
                "x": int(max(bbox.x1, 0)),
                "y": int(max(bbox.y1, 0)),
                "w": int(max(0, bbox.x2 - bbox.x1)),
                "h": int(max(0, bbox.y2 - bbox.y1)),
            }
            cropped_plate = crop_ocr_plate(img, tight_box)
            if cropped_plate is None or cropped_plate.size == 0:
                x1, y1 = tight_box["x"], tight_box["y"]
                x2 = min(tight_box["x"] + tight_box["w"], img.shape[1])
                y2 = min(tight_box["y"] + tight_box["h"], img.shape[0])
                cropped_plate = img[y1:y2, x1:x2]
            ocr_result = self.ocr.predict(cropped_plate)
            alpr_results.append(ALPRResult(detection=detection, ocr=ocr_result))
        return alpr_results


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
        "Initializing ALPR detector=%s conf=%.2f ocr=%s paddle_langs=%s langs=%s gpu=%s ocr_pad_x=%s ocr_pad_y=%s",
        detector_model,
        detector_conf,
        plate_ocr_backend(),
        paddle_ocr_lang(),
        plate_ocr_langs(),
        gpu,
        os.environ.get("PLATE_OCR_BOX_PAD_X", "0.22"),
        os.environ.get("PLATE_OCR_BOX_PAD_Y", "0.15"),
    )
    return PaddedALPR(**kwargs)


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
