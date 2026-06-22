"""OCR regression tests on UAE_PLATES/ images.

Run (loads YOLO + OCR models — slow):
  RUN_UAE_PLATE_TESTS=1 pytest tests/test_uae_plates.py -s -v

PowerShell:
  $env:RUN_UAE_PLATE_TESTS=1; pytest tests/test_uae_plates.py -s -v

Discovery mode (no expected values in manifest): the test always passes and
prints what each image's best OCR read was — use this to populate manifest.json.

Assertion mode: set a non-empty "expected" string in UAE_PLATES/manifest.json
for each image; the test then asserts at least one detection matches.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers.plate_cluster import plates_similar
from helpers.plate_pipeline import detect_plates_in_image

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "UAE_PLATES"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_UAE_PLATE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="Set RUN_UAE_PLATE_TESTS=1 to run UAE plate OCR tests",
)


def _load_fixtures() -> list[dict]:
    if not MANIFEST_PATH.is_file():
        # Fall back: discover all images in folder.
        images = sorted(
            p for p in FIXTURE_DIR.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
        )
        return [{"image": p.name, "expected": "", "path": p} for p in images]

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    out: list[dict] = []
    for item in data.get("fixtures") or []:
        path = FIXTURE_DIR / str(item["image"])
        if not path.is_file():
            pytest.skip(f"Missing fixture image: {path}")
        out.append({**item, "path": path})
    return out


def _best_detection(detections: list[dict]) -> dict | None:
    if not detections:
        return None
    return max(detections, key=lambda d: float(d.get("confidence") or 0))


def _matches_expected(detections: list[dict], expected: str) -> bool:
    for det in detections:
        got = str(det.get("plate_normalized") or "")
        if got == expected or plates_similar(got, expected):
            return True
    return False


@pytest.fixture(scope="module")
def fixtures() -> list[dict]:
    items = _load_fixtures()
    if not items:
        pytest.skip(f"No images found in {FIXTURE_DIR}")
    return items


def test_uae_plates_detection(fixtures, capsys):
    """Run detect+OCR on each UAE plate image; print scorecard; assert expected plates are found."""
    rows: list[dict] = []

    for item in fixtures:
        detections = detect_plates_in_image(str(item["path"]))
        best = _best_detection(detections)
        expected = str(item.get("expected") or "").strip()
        got = str(best.get("plate_normalized") or "") if best else ""
        conf = float(best.get("confidence") or 0) if best else 0.0
        color = str(best.get("plate_color") or "") if best else ""
        all_reads = [str(d.get("plate_normalized") or "") for d in detections if d.get("plate_normalized")]

        match = _matches_expected(detections, expected) if expected else None

        rows.append({
            "image": item["image"],
            "expected": expected,
            "got": got or "(none)",
            "conf": conf,
            "color": color,
            "all_reads": all_reads,
            "match": match,
        })

    print("\n--- UAE Plate OCR Results ---")
    print(f"{'IMAGE':<20} {'EXPECTED':<14} {'TOP READ':<14} {'CONF':>5}  {'COLOR':<10} {'STATUS':<8} ALL READS")
    print("-" * 100)
    for r in rows:
        if r["match"] is None:
            status = "?"
        elif r["match"]:
            status = "OK"
        else:
            status = "MISS"
        all_str = ", ".join(r["all_reads"]) if r["all_reads"] else "(none)"
        print(
            f"{r['image']:<20} {r['expected'] or '-':<14} {r['got']:<14} "
            f"{r['conf']:5.2f}  {r['color']:<10} {status:<8} {all_str}"
        )
    print("-" * 100)

    # Only assert for images that have an expected value set in the manifest.
    assert_rows = [r for r in rows if r["expected"]]
    if not assert_rows:
        print("Discovery mode: no expected values set — populate UAE_PLATES/manifest.json to enable assertions.")
        return

    hits = sum(1 for r in assert_rows if r["match"])
    total = len(assert_rows)
    print(f"Match: {hits}/{total}")
    assert hits == total, (
        f"{total - hits}/{total} expected UAE plates not detected; see scorecard above"
    )


@pytest.mark.parametrize("image_name", [
    "IMAGE_TEST1.jpg",
    "image_test2.jpg",
    "imagetest3.jpg",
    "imagetest4.png",
])
def test_uae_plate_pipeline_runs_without_error(image_name):
    """Verify the pipeline completes without raising on each UAE plate image."""
    image_path = FIXTURE_DIR / image_name
    if not image_path.is_file():
        pytest.skip(f"Image not found: {image_path}")
    detections = detect_plates_in_image(str(image_path))
    assert isinstance(detections, list), "detect_plates_in_image must return a list"
    for det in detections:
        assert "plate_text" in det
        assert "plate_normalized" in det
        assert "confidence" in det
        assert isinstance(det["confidence"], float)
        assert 0.0 <= det["confidence"] <= 1.0


def test_uae_plates_all_images_found():
    """Sanity check: all images listed in manifest exist on disk."""
    if not MANIFEST_PATH.is_file():
        pytest.skip("No manifest.json — skipping existence check")
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    missing = []
    for item in data.get("fixtures") or []:
        p = FIXTURE_DIR / str(item["image"])
        if not p.is_file():
            missing.append(str(p))
    assert not missing, f"Missing UAE plate images: {missing}"
