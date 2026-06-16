"""Minimal OCR cleanup: strip junk, uppercase — no rewriting or pattern guessing."""

from __future__ import annotations

import os
import re

from helpers.plate_normalize import normalize_plate

# Note: "|" and "`" are NOT junk — OCR often emits them for capital "I"; mapped in fix_ocr_confusions().
_JUNK_CHARS_RE = re.compile(r"[=_\-*/\\.+:;,\"'<>()\[\]{}~!@#$%^&*?]+")
_ALNUM_PLATE = re.compile(r"^[A-Z0-9\u0600-\u06FF]+$")

# Thin-letter confusions before junk stripping (cross-border safe — no format templates).
_OCR_CHAR_FIXES = str.maketrans({
    "|": "I",
    "`": "I",
    "¦": "I",
    "ǀ": "I",
    "ℐ": "I",
})


def plate_min_length() -> int:
    return max(4, int(os.environ.get("PLATE_MIN_LENGTH", "4")))


def plate_max_length() -> int:
    return min(14, max(plate_min_length(), int(os.environ.get("PLATE_MAX_LENGTH", "12"))))


def fix_ocr_confusions(raw: str | None) -> str:
    """Map common misreads (e.g. pipe → capital I) before junk removal."""
    if not raw:
        return ""
    return raw.strip().translate(_OCR_CHAR_FIXES)


def ocr_read_variants(cleaned: str) -> list[str]:
    """
    Alternate reads for visually ambiguous characters — both forms kept for ranking
    so the multi-frame tracker vote resolves the ambiguity rather than a heuristic.

    Pairs handled: O↔0  I↔1  D↔O(between letters)
    Each substitution is made independently; the result set stays small because
    we only generate one substitution per character position.
    """
    if not cleaned:
        return []
    text = clean_plate_ocr_text(cleaned)
    if not text or not text.isascii():
        return [text] if text else []

    _FLIP: dict[str, str] = {"O": "0", "0": "O", "I": "1", "1": "I", "D": "O"}

    variants: set[str] = {text}
    chars = list(text)
    for i, ch in enumerate(chars):
        flip = _FLIP.get(ch)
        if flip is None:
            continue
        # D↔O only makes sense between two letters (keep the original guard).
        if ch in ("D", "O") and flip in ("D", "O"):
            left = chars[i - 1] if i > 0 else ""
            right = chars[i + 1] if i + 1 < len(chars) else ""
            if not (left.isalpha() and right.isalpha()):
                continue
        chars[i] = flip
        variants.add("".join(chars))
        chars[i] = ch  # restore
    return list(variants)


def sanitize_plate_ocr_text(raw: str | None) -> str:
    """Drop divider junk and spaces; keep what OCR returned."""
    if not raw:
        return ""
    text = fix_ocr_confusions(raw)
    text = _JUNK_CHARS_RE.sub("", text)
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


def rank_ocr_candidate(text: str, confidence: float) -> float:
    """
    Rank OCR candidates: primary = confidence, small tie-break for longer text
    (dropped thin letters like I shorten the string at similar confidence).
    """
    base = max(0.0, min(1.0, float(confidence)))
    length_bonus = min(0.06, max(0, len(text) - 4) * 0.008)
    return min(1.0, base + length_bonus)
