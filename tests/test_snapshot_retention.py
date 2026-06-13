import os
import time

from helpers import snapshot_retention as sr


def test_purge_old_snapshots_removes_expired_files(tmp_path, monkeypatch):
    folder = tmp_path / "crops"
    folder.mkdir()
    old_file = folder / "old.jpg"
    new_file = folder / "new.jpg"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")

    old_ts = time.time() - (100 * 86400)
    os.utime(old_file, (old_ts, old_ts))

    monkeypatch.setattr(sr, "snapshot_retention_days", lambda: 90)
    monkeypatch.setattr(sr, "_snapshot_folders", lambda: (str(folder),))

    removed = sr.purge_old_snapshots()

    assert removed == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_purge_old_snapshots_disabled_when_retention_zero(monkeypatch, tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    stale = folder / "stale.jpg"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - (200 * 86400), time.time() - (200 * 86400)))

    monkeypatch.setattr(sr, "snapshot_retention_days", lambda: 0)
    monkeypatch.setattr(sr, "_snapshot_folders", lambda: (str(folder),))

    assert sr.purge_old_snapshots() == 0
    assert stale.exists()
