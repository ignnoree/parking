import time

from helpers import live_frame_buffer as lfb


def test_flash_logged_plates_expire(monkeypatch):
    monkeypatch.setattr(lfb, "live_log_overlay_seconds", lambda: 0.2)
    lfb.clear_stream_buffer()
    lfb.flash_logged_plates(
        [
            {
                "plate_text": "ABC1234",
                "box": {"x": 10, "y": 20, "w": 80, "h": 30},
                "match_status": "unregistered",
            }
        ]
    )
    assert lfb.get_stream_status()["logged_flashes"] == 1
    time.sleep(0.25)
    assert lfb.get_stream_status()["logged_flashes"] == 0
