import datetime
from unittest.mock import patch

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


def test_format_wrap_suffix_includes_track_stats():
    suffix = pl._format_wrap_suffix(
        {"wrap_s": 3.42, "ocr_elapsed_s": 2.1, "track_hits": 4}
    )
    assert "wrap_s=3.42s" in suffix
    assert "ocr_s=2.1s" in suffix
    assert "hits=4" in suffix


def test_log_parking_events_includes_wrap_s(monkeypatch):
    monkeypatch.setattr(pl, "PARKING_READ_STABILITY_COUNT", 1)
    monkeypatch.setattr(pl, "PARKING_JITTER_COOLDOWN_SECONDS", 0)
    pl._read_history.clear()
    pl._last_parking_log_at.clear()
    pl._recent_unregistered_logs.clear()

    payload = {
        "status": "ok",
        "direction": "entry",
        "results": [
            {
                "plate_text": "AB12CDE",
                "plate_normalized": "AB12CDE",
                "confidence": 0.9,
                "match_status": "unregistered",
                "vehicle_id": None,
                "box": {"x": 1, "y": 2, "w": 3, "h": 4},
            }
        ],
    }

    with (
        patch.object(pl, "_persist_source_frame", return_value="source.jpg"),
        patch.object(pl, "_persist_plate_crop", return_value="crop.jpg"),
        patch.object(pl, "log_parking_event"),
        patch.object(pl.logging, "info") as info_mock,
    ):
        pl.log_parking_events_for_results("/tmp/frame.jpg", payload, wrap_started_at=1000.0)

    log_call = next(
        call
        for call in info_mock.call_args_list
        if call.args and str(call.args[0]).startswith("[PARKING_LOGGED]")
    )
    assert "wrap_s=" in str(log_call.args[-1])


def test_log_uncertain_track_event_persists_uncertain_status(monkeypatch):
    monkeypatch.setenv("PARKING_LOG_UNCERTAIN_ENABLED", "true")
    monkeypatch.setenv("PARKING_JITTER_COOLDOWN_SECONDS", "0")
    pl._recent_uncertain_logs.clear()
    pl._recent_unregistered_logs.clear()
    pl._last_parking_log_at.clear()

    with (
        patch.object(pl, "_persist_source_frame", return_value="skipped/source.jpg"),
        patch.object(pl, "_persist_plate_crop", return_value="skipped/crop.jpg"),
        patch.object(pl, "log_parking_event") as log_mock,
        patch.object(pl.logging, "info"),
    ):
        ok = pl.log_uncertain_track_event(
            "/tmp/frame.jpg",
            direction="entry",
            plate_normalized="LMI3VCV",
            plate_text="LMI3VCV",
            confidence=0.67,
            box={"x": 1, "y": 2, "w": 3, "h": 4},
            timing={"track_id": 19, "wrap_s": 4.0},
            skip_reason="expired_single_uncertain",
            track_id=19,
        )

    assert ok is True
    log_mock.assert_called_once()
    assert log_mock.call_args.kwargs["match_status"] == "uncertain"


def test_log_uncertain_suppressed_after_recent_confirmed_log(monkeypatch):
    monkeypatch.setenv("PARKING_LOG_UNCERTAIN_ENABLED", "true")
    monkeypatch.setenv("PARKING_JITTER_COOLDOWN_SECONDS", "120")
    pl._recent_uncertain_logs.clear()
    pl._recent_unregistered_logs.clear()
    pl._last_parking_log_at.clear()

    now = datetime.datetime.now(datetime.timezone.utc)
    pl._recent_unregistered_logs.append(("FJI4ZHY", "entry", now - datetime.timedelta(seconds=30)))

    with (
        patch.object(pl, "_persist_source_frame", return_value="skipped/source.jpg"),
        patch.object(pl, "_persist_plate_crop", return_value="skipped/crop.jpg"),
        patch.object(pl, "log_parking_event") as log_mock,
        patch.object(pl.logging, "debug"),
    ):
        ok = pl.log_uncertain_track_event(
            "/tmp/frame.jpg",
            direction="entry",
            plate_normalized="FJI4ZHY",
            confidence=0.78,
            skip_reason="expired_single_uncertain",
        )

    assert ok is False
    log_mock.assert_not_called()


def test_log_uncertain_suppressed_by_parking_log_cooldown(monkeypatch):
    monkeypatch.setenv("PARKING_LOG_UNCERTAIN_ENABLED", "true")
    monkeypatch.setenv("PARKING_JITTER_COOLDOWN_SECONDS", "0")
    monkeypatch.setattr(pl, "parking_log_cooldown_seconds", lambda: 600)
    pl._recent_uncertain_logs.clear()
    pl._recent_unregistered_logs.clear()
    pl._last_parking_log_at.clear()

    now = datetime.datetime.now(datetime.timezone.utc)
    pl._last_parking_log_at["unregistered:FJI4ZHY:entry"] = now - datetime.timedelta(seconds=180)

    with (
        patch.object(pl, "_persist_source_frame", return_value="skipped/source.jpg"),
        patch.object(pl, "_persist_plate_crop", return_value="skipped/crop.jpg"),
        patch.object(pl, "log_parking_event") as log_mock,
        patch.object(pl.logging, "debug"),
    ):
        ok = pl.log_uncertain_track_event(
            "/tmp/frame.jpg",
            direction="entry",
            plate_normalized="FJI4ZHY",
            confidence=0.78,
            skip_reason="vote_uncertain",
        )

    assert ok is False
    log_mock.assert_not_called()


def test_confirmed_suppressed_after_recent_uncertain_log(monkeypatch):
    monkeypatch.setattr(pl, "PARKING_READ_STABILITY_COUNT", 1)
    monkeypatch.setenv("PARKING_JITTER_COOLDOWN_SECONDS", "120")
    pl._read_history.clear()
    pl._last_parking_log_at.clear()
    pl._recent_unregistered_logs.clear()
    pl._recent_uncertain_logs.clear()

    now = datetime.datetime.now(datetime.timezone.utc)
    pl._recent_uncertain_logs.append(("WR02FKD", "entry", now - datetime.timedelta(seconds=30)))

    payload = {
        "status": "ok",
        "direction": "entry",
        "results": [
            {
                "plate_text": "WR02FKD",
                "plate_normalized": "WR02FKD",
                "confidence": 0.82,
                "match_status": "unregistered",
                "vehicle_id": None,
                "track_confirmed": True,
                "box": {"x": 1, "y": 2, "w": 3, "h": 4},
            }
        ],
    }

    with (
        patch.object(pl, "_persist_source_frame", return_value="source.jpg"),
        patch.object(pl, "_persist_plate_crop", return_value="crop.jpg"),
        patch.object(pl, "log_parking_event") as log_mock,
        patch.object(pl.logging, "info"),
    ):
        logged = pl.log_parking_events_for_results("/tmp/frame.jpg", payload)

    assert logged == []
    log_mock.assert_not_called()
