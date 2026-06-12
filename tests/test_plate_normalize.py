from helpers.plate_normalize import normalize_plate


def test_normalize_strips_spaces():
    assert normalize_plate("12 B 34567") == normalize_plate("12B34567")


def test_normalize_uppercase():
    assert normalize_plate("abc1234") == "ABC1234"
