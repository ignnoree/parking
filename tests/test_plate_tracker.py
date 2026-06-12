import pytest

from helpers.plate_tracker import PlateTracker, box_iou, nms_detections


@pytest.fixture(autouse=True)
def _track_env(monkeypatch):
    monkeypatch.setenv("PLATE_TRACK_MIN_HITS", "1")
    monkeypatch.setenv("PLATE_TRACK_VOTE_COUNT", "2")


def test_box_iou_identical():
    box = {"x": 10, "y": 20, "w": 100, "h": 30}
    assert box_iou(box, box) == 1.0


def test_box_iou_disjoint():
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 50, "y": 50, "w": 10, "h": 10}
    assert box_iou(a, b) == 0.0


def test_nms_drops_overlapping_boxes():
    detections = [
        {"box": {"x": 0, "y": 0, "w": 100, "h": 30}, "confidence": 0.9},
        {"box": {"x": 5, "y": 2, "w": 95, "h": 28}, "confidence": 0.7},
        {"box": {"x": 300, "y": 0, "w": 80, "h": 25}, "confidence": 0.85},
    ]
    kept = nms_detections(detections, iou_threshold=0.45)
    assert len(kept) == 2
    assert float(kept[0]["confidence"]) == 0.9


def test_tracker_assigns_new_ids():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=2.0)
    box = [{"box": {"x": 0, "y": 0, "w": 50, "h": 20}, "confidence": 0.9}]
    first = tracker.update(box, now=1.0)
    assert len(first) == 1
    assert first[0].track_id == 1

    second = tracker.update(
        [{"box": {"x": 2, "y": 1, "w": 50, "h": 20}, "confidence": 0.88}],
        now=1.1,
    )
    assert len(second) == 1
    assert second[0].track_id == 1


def test_tracker_separates_two_plates():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=2.0)
    need = tracker.update(
        [
            {"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9},
            {"box": {"x": 200, "y": 0, "w": 40, "h": 20}, "confidence": 0.91},
        ],
        now=0.0,
    )
    assert len(need) == 2
    ids = {track.track_id for track in tracker.active_tracks()}
    assert len(ids) == 2


def test_tracker_instant_log_on_high_confidence(monkeypatch):
    monkeypatch.setenv("PLATE_TRACK_INSTANT_LOG_CONF", "0.65")
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=2.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    read = {"plate_normalized": "BDB4668", "plate_text": "BDB4668", "confidence": 0.7}
    tracker.record_ocr_read(1, read)
    stable = tracker.stable_read_for_logging(1)
    assert stable is not None
    assert stable["plate_normalized"] == "BDB4668"


def test_tracker_vote_requires_two_agreeing_reads(monkeypatch):
    monkeypatch.setenv("PLATE_TRACK_INSTANT_LOG_CONF", "0.80")
    tracker = PlateTracker(
        iou_threshold=0.3,
        max_age_seconds=2.0,
        ocr_max_attempts=4,
        ocr_interval_seconds=0.5,
    )
    read_a = {"plate_normalized": "BDB4668", "plate_text": "BDB4668", "confidence": 0.7}
    read_b = {"plate_normalized": "BBT94668", "plate_text": "BBT94668", "confidence": 0.6}
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    tracker.record_ocr_read(1, read_a)
    assert tracker.stable_read_for_logging(1) is None
    tracker.record_ocr_read(1, read_b)
    assert tracker.stable_read_for_logging(1) is None
    tracker.record_ocr_read(1, read_a)
    stable = tracker.stable_read_for_logging(1)
    assert stable is not None
    assert stable["plate_normalized"] == "BDB4668"


def test_tracker_retries_ocr_until_confirmed():
    tracker = PlateTracker(
        iou_threshold=0.3,
        max_age_seconds=2.0,
        ocr_max_attempts=3,
        ocr_interval_seconds=0.5,
    )
    box = {"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}
    assert len(tracker.update([box], now=0.0)) == 1
    tracker.mark_ocr_pending(1, now=0.0)
    assert len(tracker.update([box], now=0.2)) == 0
    tracker.mark_ocr_finished(1)
    assert len(tracker.update([box], now=0.2)) == 0
    assert len(tracker.update([box], now=0.6)) == 1
    tracker.mark_confirmed(
        1,
        plate_text="AB12CDE",
        plate_normalized="AB12CDE",
        confidence=0.8,
        logged=True,
    )
    assert len(tracker.update([box], now=0.7)) == 0


def test_tracker_drops_stale_tracks():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=0.5)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    tracker.update([], now=1.0)
    assert tracker.active_tracks() == []


def test_tracker_log_timing_from_first_detection():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=2.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=10.0,
    )
    timing = tracker.log_timing(1, now=12.5)
    assert timing["wrap_s"] == 2.5
    assert timing["detect_to_log_s"] == 2.5
    assert timing["track_id"] == 1


def test_track_for_ocr_text_finds_similar_track():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    tracker.record_ocr_read(1, {"plate_normalized": "AP05JEO", "confidence": 0.65})

    match = tracker.track_for_ocr_text("AP05JED")
    assert match is not None
    assert match.track_id == 1


def test_track_for_ocr_text_ignores_logged_tracks():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    tracker.record_ocr_read(1, {"plate_normalized": "AP05JEO", "confidence": 0.65})
    tracker.mark_confirmed(
        1,
        plate_text="AP05JEO",
        plate_normalized="AP05JEO",
        confidence=0.65,
        logged=True,
    )

    assert tracker.track_for_ocr_text("AP05JED") is None


def test_track_for_ocr_text_picks_highest_confidence_match():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [
            {"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9},
            {"box": {"x": 200, "y": 0, "w": 40, "h": 20}, "confidence": 0.9},
        ],
        now=0.0,
    )
    tracker.record_ocr_read(1, {"plate_normalized": "AP05JEO", "confidence": 0.55})
    tracker.record_ocr_read(2, {"plate_normalized": "AP05JED", "confidence": 0.78})

    match = tracker.track_for_ocr_text("AP05JEO")
    assert match is not None
    assert match.track_id == 2


def test_track_for_ocr_text_skips_dissimilar_reads():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    tracker.record_ocr_read(1, {"plate_normalized": "AP05JEO", "confidence": 0.65})

    assert tracker.track_for_ocr_text("XYZ9999") is None


def test_merge_into_combines_reads_and_deletes_source():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [
            {"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9},
            {"box": {"x": 500, "y": 0, "w": 40, "h": 20}, "confidence": 0.85},
        ],
        now=0.0,
    )
    tracker.record_ocr_read(1, {"plate_normalized": "AP05JEO", "confidence": 0.65})
    tracker.update(
        [{"box": {"x": 510, "y": 5, "w": 40, "h": 20}, "confidence": 0.88}],
        now=0.5,
    )
    tracker.record_ocr_read(2, {"plate_normalized": "AP05JED", "confidence": 0.72})

    assert tracker.merge_into(2, 1) is True
    assert tracker.get_track(2) is None
    merged = tracker.get_track(1)
    assert merged is not None
    norms = [r["plate_normalized"] for r in merged.ocr_reads]
    assert norms == ["AP05JEO", "AP05JED"]
    assert merged.hits >= 2


def test_merge_into_chooses_freshest_position():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    tracker.update(
        [{"box": {"x": 200, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=1.0,
    )
    assert tracker.merge_into(2, 1) is True
    merged = tracker.get_track(1)
    assert merged is not None
    # Latest position came from track 2 (last_seen=1.0).
    assert merged.box["x"] == 200
    assert merged.last_seen == 1.0


def test_merge_into_noop_when_ids_match_or_missing():
    tracker = PlateTracker(iou_threshold=0.3, max_age_seconds=10.0)
    tracker.update(
        [{"box": {"x": 0, "y": 0, "w": 40, "h": 20}, "confidence": 0.9}],
        now=0.0,
    )
    assert tracker.merge_into(1, 1) is False
    assert tracker.merge_into(1, 999) is False
    assert tracker.merge_into(999, 1) is False
    assert tracker.get_track(1) is not None
