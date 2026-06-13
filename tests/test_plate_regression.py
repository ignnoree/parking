"""OCR regression on real parking log source frames (test_images/plates/).

Run (slow — loads YOLO + OCR models):
  RUN_PLATE_REGRESSION=1 pytest tests/test_plate_regression.py -s -v

Or on Windows PowerShell:
  $env:RUN_PLATE_REGRESSION=1; pytest tests/test_plate_regression.py -s -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers.plate_cluster import plates_similar
from helpers.plate_pipeline import run_plate_detect_on_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "test_images" / "plates"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PLATE_REGRESSION", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="Set RUN_PLATE_REGRESSION=1 to run slow plate OCR regression",
)


def _load_manifest() -> list[dict]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures = data.get("fixtures") or []
    out: list[dict] = []
    for item in fixtures:
        image = FIXTURE_DIR / str(item["image"])
        if not image.is_file():
            pytest.skip(f"Missing fixture image: {image}")
        out.append({**item, "path": image})
    return out


def _reads_matching(payload: dict, expected: str) -> list[dict]:
    matches: list[dict] = []
    for row in payload.get("results") or []:
        got = str(row.get("plate_normalized") or "")
        if not got:
            continue
        if got == expected or plates_similar(got, expected):
            matches.append(row)
    return matches


def _all_reads(payload: dict) -> list[str]:
    return [
        str(row.get("plate_normalized") or "")
        for row in (payload.get("results") or [])
        if row.get("plate_normalized")
    ]


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    if not MANIFEST_PATH.is_file():
        pytest.skip(f"Missing manifest: {MANIFEST_PATH}")
    return _load_manifest()


def test_plate_regression_accuracy(manifest, capsys):
    """Run full detect+OCR on each fixture; print scorecard vs expected plates."""
    rows: list[dict] = []
    for item in manifest:
        payload = run_plate_detect_on_file(str(item["path"]), direction="entry")
        expected = str(item["expected"])
        hits = _reads_matching(payload, expected)
        all_norms = _all_reads(payload)
        best = None
        if payload.get("results"):
            best = max(payload["results"], key=lambda row: float(row.get("confidence") or 0))
        got = str(best.get("plate_normalized") or "") if best else ""
        exact = any(str(h.get("plate_normalized") or "") == expected for h in hits)
        fuzzy = bool(hits)
        rows.append(
            {
                "image": item["image"],
                "expected": expected,
                "logged": item.get("logged"),
                "got": got or "(none)",
                "all_in_frame": all_norms,
                "conf": float(best.get("confidence") or 0) if best else 0.0,
                "exact": exact,
                "fuzzy": fuzzy,
            }
        )

    exact_hits = sum(1 for r in rows if r["exact"])
    fuzzy_hits = sum(1 for r in rows if r["fuzzy"])
    total = len(rows)

    print("\n--- Plate OCR regression (test_images/plates) ---")
    print(f"{'IMAGE':<16} {'EXPECTED':<10} {'TOP READ':<12} {'CONF':>5}  {'OK':<5} ALL IN FRAME")
    print("-" * 78)
    for r in rows:
        mark = "OK" if r["exact"] else ("~" if r["fuzzy"] else "MISS")
        all_reads = ", ".join(r["all_in_frame"]) if r["all_in_frame"] else "(none)"
        print(
            f"{r['image']:<16} {r['expected']:<10} {r['got']:<12} "
            f"{r['conf']:5.2f}  {mark:<5} {all_reads}"
        )
    print("-" * 78)
    print(f"Exact (any detection in frame): {exact_hits}/{total}")
    print(f"Fuzzy match in frame: {fuzzy_hits}/{total}")

    assert exact_hits >= max(1, total // 2), (
        f"Only {exact_hits}/{total} plates matched exactly; see scorecard above"
    )
