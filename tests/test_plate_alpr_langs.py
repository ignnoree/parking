from unittest.mock import Mock

import numpy as np
from fast_alpr.base import OcrResult

from helpers import plate_alpr as alpr


def test_paddle_ocr_langs_default_en_ar(monkeypatch):
    monkeypatch.delenv("PLATE_PADDLE_LANGS", raising=False)
    monkeypatch.delenv("PLATE_PADDLE_LANG", raising=False)
    monkeypatch.setenv("PLATE_OCR_LANGS", "en,ar")
    assert alpr.paddle_ocr_langs() == ["en", "ar"]


def test_paddle_ocr_langs_override(monkeypatch):
    monkeypatch.setenv("PLATE_PADDLE_LANGS", "ar,en")
    assert alpr.paddle_ocr_langs() == ["ar", "en"]


def test_easyocr_langs_maps_tokens(monkeypatch):
    monkeypatch.setenv("PLATE_OCR_LANGS", "english,arabic")
    assert alpr.easyocr_langs() == ["en", "ar"]


def test_default_backend_is_bilingual(monkeypatch):
    monkeypatch.delenv("PLATE_OCR_BACKEND", raising=False)
    assert alpr.plate_ocr_backend() == "bilingual"


def test_early_exit_hit_requires_plausible_and_confident():
    good = OcrResult(text="TS07JS9670", confidence=0.9)
    low_conf = OcrResult(text="TS07JS9670", confidence=0.5)
    no_digits = OcrResult(text="ABCDEFG", confidence=0.95)
    assert alpr._early_exit_hit([good]) is True
    assert alpr._early_exit_hit([low_conf]) is False
    assert alpr._early_exit_hit([no_digits]) is False
    assert alpr._early_exit_hit([low_conf, no_digits, good]) is True


def test_early_exit_disabled_when_conf_zero(monkeypatch):
    monkeypatch.setenv("PLATE_OCR_EARLY_EXIT_CONF", "0")
    good = OcrResult(text="TS07JS9670", confidence=0.99)
    assert alpr._early_exit_hit([good]) is False


def test_bilingual_early_exit_skips_second_language():
    backend = object.__new__(alpr.BilingualPaddleOcrBackend)
    first = Mock()
    first.collect_candidates.return_value = [OcrResult(text="TS07JS9670", confidence=0.9)]
    second = Mock()
    backend._backends = [first, second]
    backend._langs = ["en", "ar"]

    crop = np.zeros((20, 60, 3), dtype=np.uint8)
    candidates = backend.collect_candidates(crop)

    assert [c.text for c in candidates] == ["TS07JS9670"]
    second.collect_candidates.assert_not_called()


def test_bilingual_runs_second_language_without_good_read():
    backend = object.__new__(alpr.BilingualPaddleOcrBackend)
    first = Mock()
    first.collect_candidates.return_value = [OcrResult(text="GARBAGE", confidence=0.3)]
    second = Mock()
    second.collect_candidates.return_value = [OcrResult(text="TS07JS9670", confidence=0.8)]
    backend._backends = [first, second]
    backend._langs = ["en", "ar"]

    crop = np.zeros((20, 60, 3), dtype=np.uint8)
    candidates = backend.collect_candidates(crop)

    assert len(candidates) == 2
    second.collect_candidates.assert_called_once()


def test_backend_tiered_selected(monkeypatch):
    monkeypatch.setenv("PLATE_OCR_BACKEND", "tiered")
    assert alpr.plate_ocr_backend() == "tiered"


def test_tiered_skips_paddle_on_confident_fast_read():
    backend = object.__new__(alpr.TieredOcrBackend)
    backend._fast = Mock()
    backend._fast.collect_candidates.return_value = [OcrResult(text="TS07JS9670", confidence=0.9)]
    backend._paddle = Mock()

    crop = np.zeros((20, 60, 3), dtype=np.uint8)
    candidates = backend.collect_candidates(crop)

    assert [c.text for c in candidates] == ["TS07JS9670"]
    backend._paddle.collect_candidates.assert_not_called()


def test_tiered_falls_back_to_paddle_on_weak_fast_read():
    backend = object.__new__(alpr.TieredOcrBackend)
    backend._fast = Mock()
    backend._fast.collect_candidates.return_value = [OcrResult(text="QBO5EI", confidence=0.4)]
    backend._paddle = Mock()
    backend._paddle.collect_candidates.return_value = [OcrResult(text="B98E02", confidence=0.8)]

    crop = np.zeros((20, 60, 3), dtype=np.uint8)
    candidates = backend.collect_candidates(crop)

    assert len(candidates) == 2
    backend._paddle.collect_candidates.assert_called_once()


def test_ambiguity_variants_handles_per_char_confidence_list():
    base = OcrResult(text="DB84668", confidence=[0.71, 0.68, 0.65, 0.7, 0.72, 0.69, 0.67])
    expanded = alpr._with_ambiguity_variants([base])
    assert expanded[0].confidence == [0.71, 0.68, 0.65, 0.7, 0.72, 0.69, 0.67]
    for variant in expanded[1:]:
        assert isinstance(variant.confidence, float)
        assert 0.0 < variant.confidence < 1.0
