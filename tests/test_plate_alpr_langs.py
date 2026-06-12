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
