import datetime

from helpers import parking_logging as pl


def test_plates_similar_edit_distance():
    assert pl._plates_similar("ABC1234", "ABC1235") is True


def test_stable_unregistered_requires_repeats(monkeypatch):
    monkeypatch.setattr(pl, "PARKING_READ_STABILITY_COUNT", 2)
    monkeypatch.setattr(pl, "PARKING_READ_STABILITY_WINDOW_SECONDS", 8.0)
    pl._read_history.clear()
    now = datetime.datetime.now(datetime.timezone.utc)
    assert pl._stable_unregistered_read("TEST1234", now) is False
    pl._read_history.append(("TEST1234", now))
    assert pl._stable_unregistered_read("TEST1234", now) is True
