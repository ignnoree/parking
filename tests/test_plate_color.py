import numpy as np

from helpers.plate_color import classify_plate_background_color


def _solid_crop(bgr: tuple[int, int, int], *, w: int = 120, h: int = 40) -> np.ndarray:
    crop = np.zeros((h, w, 3), dtype=np.uint8)
    crop[:, :] = bgr
    # Dark text band in the center so border sampling ignores it.
    crop[h // 3 : 2 * h // 3, w // 4 : 3 * w // 4] = (20, 20, 20)
    return crop


def test_classify_white_plate():
    crop = _solid_crop((245, 245, 245))
    assert classify_plate_background_color(crop) == "white"


def test_classify_cool_tinted_white_not_blue():
    # Slight blue cast from lighting/JPEG — still a white plate.
    crop = _solid_crop((235, 242, 252))
    assert classify_plate_background_color(crop) == "white"


def test_classify_dim_white_not_unknown():
    crop = _solid_crop((180, 182, 186))
    assert classify_plate_background_color(crop) in {"white", "unknown"}


def test_classify_red_plate():
    crop = _solid_crop((30, 30, 220))
    assert classify_plate_background_color(crop) == "red"


def test_classify_green_plate():
    crop = _solid_crop((40, 180, 60))
    assert classify_plate_background_color(crop) == "green"


def test_classify_empty_crop():
    assert classify_plate_background_color(None) == "unknown"
