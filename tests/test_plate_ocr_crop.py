from helpers.plate_crop import expand_ocr_plate_box


def test_expand_ocr_plate_box_widens_tight_box():
    tight = {"x": 100, "y": 50, "w": 200, "h": 40}
    expanded = expand_ocr_plate_box(tight, frame_width=800, frame_height=600)
    assert expanded is not None
    assert expanded["x"] < tight["x"]
    assert expanded["x"] + expanded["w"] > tight["x"] + tight["w"]
    assert expanded["y"] < tight["y"]
    assert expanded["y"] + expanded["h"] > tight["y"] + tight["h"]
