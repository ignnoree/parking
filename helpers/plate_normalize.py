"""Normalize plate strings for DB lookup (Latin, Arabic digits, Persian variants)."""

from __future__ import annotations

import re

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_PERSIAN_YEH = str.maketrans({"ی": "ي", "ک": "ك"})


def normalize_plate(raw: str | None) -> str:
    if not raw:
        return ""
    text = raw.strip().translate(_PERSIAN_DIGITS).translate(_ARABIC_DIGITS)
    text = text.translate(_PERSIAN_YEH)
    text = re.sub(r"[\s\-_]+", "", text)
    return text.upper()
