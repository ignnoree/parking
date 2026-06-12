import datetime

from helpers.plate_cluster import canonical_plate, plates_similar, reset_plate_clusters


def test_plates_similar_suffix_digits():
    assert plates_similar("TS07JS9670", "S07JS9670") is True
    assert plates_similar("ATS07J9670", "TS07JS9670") is True


def test_canonical_plate_clusters_similar_reads():
    reset_plate_clusters()
    now = datetime.datetime.now(datetime.timezone.utc)
    a = canonical_plate("TS07JS9670", now)
    b = canonical_plate("S07JS9670", now)
    assert a == b
