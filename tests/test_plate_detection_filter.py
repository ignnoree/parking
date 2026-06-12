from helpers.plate_detection_filter import filter_plate_detections
from helpers.plate_format import is_plausible_plate


def test_filter_drops_square_wall_box():
    detections = [
        {"box": {"x": 0, "y": 0, "w": 100, "h": 90}, "confidence": 0.9},
        {"box": {"x": 0, "y": 0, "w": 120, "h": 30}, "confidence": 0.8},
    ]
    kept = filter_plate_detections(detections, (480, 640, 3))
    assert len(kept) == 1
    assert kept[0]["box"]["w"] == 120


def test_filter_drops_tiny_box():
    detections = [{"box": {"x": 0, "y": 0, "w": 20, "h": 10}, "confidence": 0.9}]
    assert filter_plate_detections(detections, (480, 640, 3)) == []


def test_reject_short_digit_only():
    assert is_plausible_plate("5701") is False


def test_reject_repeated_char_run():
    assert is_plausible_plate("MF66666") is False


def test_accept_indian_style_plate():
    assert is_plausible_plate("TS07JS9670") is True
