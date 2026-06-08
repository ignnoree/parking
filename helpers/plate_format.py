"""Minimal OCR cleanup: strip junk, uppercase — no rewriting or pattern guessing."""

from __future__ import annotations

import os
import re

from helpers.plate_normalize import normalize_plate

_JUNK_CHARS_RE = re.compile(r"[=_\-|*/\\.+:;,\"'<>()\[\]{}`~!@#$%^&*?]+")
_ALNUM_PLATE = re.compile(r"^[A-Z0-9\u0600-\u06FF]+$")


def plate_min_length() -> int:
    return max(4, int(os.environ.get("PLATE_MIN_LENGTH", "4")))


def plate_max_length() -> int:
    return min(14, max(plate_min_length(), int(os.environ.get("PLATE_MAX_LENGTH", "12"))))


def sanitize_plate_ocr_text(raw: str | None) -> str:
    """Drop divider junk and spaces; keep what OCR returned."""
    if not raw:
        return ""
    text = _JUNK_CHARS_RE.sub("", raw.strip())
    return re.sub(r"\s+", "", text)


def clean_plate_ocr_text(raw: str | None) -> str:
    """Sanitize + normalize for storage/lookup. Does not change letters or digits."""
    return normalize_plate(sanitize_plate_ocr_text(raw))


def plate_format_score(raw: str | None) -> float:
    """1.0 if text passes basic sanity checks, else 0."""
    return 1.0 if is_plausible_plate(raw, min_score=0.0) else 0.0


def is_plausible_plate(raw: str | None, *, min_score: float = 0.55) -> bool:
    text = clean_plate_ocr_text(raw)
    if not text:
        return False
    if "HTTP" in text or "WWW" in text or "COM" in text:
        return False
    if len(text) < plate_min_length() or len(text) > plate_max_length():
        return False
    if not _ALNUM_PLATE.match(text):
        return False
    if not any(ch.isdigit() for ch in text):
        return False
    if min_score > 0 and plate_format_score(text) < min_score:
        return False
    return True


def rank_ocr_candidate(_text: str, confidence: float) -> float:
    """Pick the OCR read with the highest engine confidence — no format guessing."""
    return max(0.0, min(1.0, float(confidence)))
