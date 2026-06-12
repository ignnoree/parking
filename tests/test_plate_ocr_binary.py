import numpy as np

from helpers.plate_ocr_preprocess import binary_threshold_variant, build_ocr_variants


def test_binary_threshold_variant_shape():
    crop = np.zeros((40, 120, 3), dtype=np.uint8)
    crop[10:30, 20:100] = 200
    out = binary_threshold_variant(crop)
    assert out.shape == crop.shape


def test_build_ocr_variants_includes_binary():
    crop = np.zeros((40, 120, 3), dtype=np.uint8)
    crop[10:30, 20:100] = 180
    binary = binary_threshold_variant(crop)
    variants = build_ocr_variants(crop)
    assert len(variants) >= 2
    assert any(v.shape == binary.shape for v in variants)
