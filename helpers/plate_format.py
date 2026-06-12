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


def fix_latin_ambiguous_chars(text: str) -> str:
    """
    Fix common Latin OCR swaps using neighbor context (cross-border safe).
    - 1 -> I when beside letters (thin I misread as one)
    - 1 -> I when adjacent to a letter in a letter-heavy plate (e.g. MW51VSU -> MWSIVSU)
    - 0 <-> O using letter vs digit neighbors
    """
    if not text or not text.isascii():
        return text
    chars = list(text.upper())
    n = len(chars)

    def latin_letter(ch: str) -> bool:
        return len(ch) == 1 and ch.isalpha() and ch.isascii()

    def digit(ch: str) -> bool:
        return len(ch) == 1 and ch.isdigit()

    other_letters = sum(1 for ch in chars if latin_letter(ch))
    other_digits = sum(1 for ch in chars if digit(ch) and ch != "1")
    letter_heavy = other_letters > other_digits

    def digit_run_length_at(idx: int) -> int:
        if not digit(chars[idx]):
            return 0
        left_count = 0
        j = idx - 1
        while j >= 0 and digit(chars[j]):
            left_count += 1
            j -= 1
        right_count = 0
        j = idx + 1
        while j < n and digit(chars[j]):
            right_count += 1
            j += 1
        return 1 + left_count + right_count

    for i in range(n):
        ch = chars[i]
        left = chars[i - 1] if i > 0 else ""
        right = chars[i + 1] if i + 1 < n else ""
        if ch == "1" and not digit(left) and not digit(right):
            if latin_letter(left) or latin_letter(right):
                chars[i] = "I"
        elif (
            ch == "1"
            and letter_heavy
            and digit_run_length_at(i) <= 2
            and (latin_letter(left) or latin_letter(right))
        ):
            chars[i] = "I"
        elif ch == "0" and latin_letter(left) and latin_letter(right):
            chars[i] = "O"
        elif ch == "O" and digit(left) and (digit(right) or right == ""):
            chars[i] = "0"
        elif ch == "O" and digit(right) and (digit(left) or left == ""):
            chars[i] = "0"
    return "".join(chars)


def ocr_read_variants(cleaned: str) -> list[str]:
    """Alternate reads for ambiguous D/O between letters (both kept for ranking)."""
    if not cleaned:
        return []
    text = clean_plate_ocr_text(cleaned)
    if not text or not text.isascii():
        return [text] if text else []
    variants = {text}
    chars = list(text)
    for i, ch in enumerate(chars):
        if i == 0 or i + 1 >= len(chars):
            continue
        left, right = chars[i - 1], chars[i + 1]
        if not (left.isalpha() and right.isalpha()):
            continue
        if ch == "O":
            chars[i] = "D"
            variants.add("".join(chars))
            chars[i] = "O"
        elif ch == "D":
            chars[i] = "O"
            variants.add("".join(chars))
            chars[i] = "D"
    return list(variants)


def fix_ocr_confusions(raw: str | None) -> str:
    """Map common misreads (e.g. pipe → capital I) before junk removal."""
    if not raw:
        return ""
    text = raw.strip().translate(_OCR_CHAR_FIXES)
    return fix_latin_ambiguous_chars(text)


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
