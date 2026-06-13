"""Clone ch production_requirements_fa_parking.md into parking with MVP ✓ / ⬜ markers."""
from __future__ import annotations

import re
from pathlib import Path

CH_DOC = Path(r"d:\Downloads\projects\ch\documents\production_requirements_fa_parking.md")
OUT_DOC = Path(r"d:\Downloads\projects\parking\documents\production_requirements_fa_parking.md")

HEADER = (
    "**وضعیت پیاده‌سازی MVP (مخزن `parking`):** "
    "در انتهای بندهای قابل‌پیاده‌سازی: `✓` = انجام‌شده · `⬜` = انجام‌نشده. "
    "(علامت ✅ ابتدای بند = محدودهٔ تولیدی در سند مرجع `ch` — بدون تغییر.)\n\n"
)

# 1-based line numbers in base doc
DONE_LINES = {
    15, 16, 17, 18, 19,
    36, 37, 38, 39, 41, 42, 44, 45, 46,
    49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 61, 62, 63, 65, 67, 68,
    71, 73, 74, 77, 78, 79, 80, 82, 83, 84, 86, 87, 88, 89, 91, 92, 94, 95, 96,
    114, 115, 116, 117, 120, 121, 122, 125, 127, 128, 129, 130,
    134, 135, 136, 137, 138, 140, 141,
    156, 157,
    176, 178, 180,
    185, 186, 187,
    191, 192, 194, 195, 196,
    204,
    223, 234, 235,
    238, 239,
    249, 250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 262, 263,
    280,
    325, 326, 327, 328, 329, 330, 331, 332, 333, 334, 335, 336,
    346, 347, 348, 349,
}

TODO_LINES = {
    20,
    60, 64, 66,
    72, 75, 76, 98, 99, 100, 101,
    104, 105, 106, 107, 108, 109, 110, 111,
    123, 124, 131,
    142, 144, 145, 147, 148, 149, 150,
    166, 167, 168, 169,
    179,
    228, 229,
    240,
    248,
    269, 270,
    277, 278,
    295,
    350,
}


def main() -> None:
    lines = CH_DOC.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []

    for i, line in enumerate(lines, start=1):
        if i == 6 and line.strip() == "":
            out.append(line)
            out.append(HEADER)
            continue

        if i in DONE_LINES:
            stripped = line.rstrip("\n\r")
            if not (stripped.endswith(" ✓") or stripped.endswith(" ⬜")):
                out.append(stripped + " ✓\n")
            else:
                out.append(line)
        elif i in TODO_LINES:
            stripped = line.rstrip("\n\r")
            if not (stripped.endswith(" ✓") or stripped.endswith(" ⬜")):
                out.append(stripped + " ⬜\n")
            else:
                out.append(line)
        else:
            out.append(line)

    text = "".join(out)

    text = text.replace(
        "| موضوع | الزام |\n|--------|--------|",
        "| موضوع | الزام | MVP |\n|--------|--------|-----|",
        1,
    )
    checklist = [
        ("نور / glare خورشید", "⬜"),
        ("دوربین توسط ادمین", "⬜"),
        ("UI مانیتور", "✓"),
        ("حذف", "✓"),
        ("Crash / exception", "✓"),
        ("کارایی", "⬜"),
        ("رنگ پلاک", "✓"),
        ("نوع وسیله (اختیاری)", "⬜"),
        ("ثبت خودرو", "✓"),
        ("مهمان", "✓"),
    ]
    for topic, mark in checklist:
        text = re.sub(
            rf"(\| {re.escape(topic)} \| [^\n|]+)\n",
            rf"\1 | {mark} |\n",
            text,
            count=1,
        )

    text = text.replace(
        "| جدول | معنی کسب‌وکاری |\n|------|----------------|",
        "| جدول | معنی کسب‌وکاری | MVP |\n|------|----------------|-----|",
        1,
    )
    for tbl, mark in [
        ("vehicles", "✓"),
        ("cameras", "⬜"),
        ("settings", "⬜"),
        ("admins", "✓"),
        ("parking_logs", "✓"),
        ("software_logs", "✓"),
    ]:
        text = re.sub(
            rf"(\| `{tbl}` \| [^\n]+)\n",
            rf"\1 | {mark} |\n",
            text,
            count=1,
        )

    text = text.replace(
        "| متغیر | کاربرد |\n|--------|--------|",
        "| متغیر | کاربرد | MVP |\n|--------|--------|-----|",
        1,
    )
    env_marks = {
        "JWT_SECRET_KEY": "✓",
        "DATABASE_URL": "✓",
        "CAMERA_URL": "✓",
        "GATE_DIRECTION": "✓",
        "PLATE_USE_GPU": "⬜",
        "PARKING_LOG_COOLDOWN_SECONDS": "✓",
        "GUEST_RETENTION_DAYS": "✓",
        "PLATE_OCR_MIN_CONFIDENCE": "✓",
        "UPLOAD_FOLDER": "✓",
        "COLLECTION_FOLDER": "✓",
        "ENV": "✓",
    }
    for var, mark in env_marks.items():
        text = re.sub(
            rf"(\| `{var}` \| [^\n]+)\n",
            rf"\1 | {mark} |\n",
            text,
            count=1,
        )
    text = re.sub(
        r"(\| `CAMERA_URL` یا `CAMERA_URL_ENTRY` / `CAMERA_URL_EXIT` \| [^\n]+)\n",
        r"\1 | ✓ |\n",
        text,
        count=1,
    )

    text = text.replace(
        "پیاده‌سازی در repo جداگانه پیش‌بینی می‌شود",
        "پیاده‌سازی MVP در مخزن `parking` — وضعیت بندها با ✓ / ⬜ در همین سند",
        1,
    )

    OUT_DOC.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOC.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_DOC} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
