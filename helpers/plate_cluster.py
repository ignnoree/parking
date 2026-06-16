"""Fuzzy plate clustering for OCR jitter (cross-border safe — no country format rules)."""

from __future__ import annotations

import collections
import datetime
import os
import threading

_lock = threading.Lock()
# Active canonical representatives for recently seen plate clusters.
_cluster_canonicals: collections.deque[tuple[str, datetime.datetime]] = collections.deque(maxlen=200)


def plate_cluster_ttl_seconds() -> int:
    return max(60, int(os.environ.get("PLATE_CLUSTER_TTL_SECONDS", "120")))


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def plate_digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def plates_similar(a: str, b: str) -> bool:
    """True when two reads are likely the same physical plate with OCR noise."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Partial-visibility suffix match: one read is the right-side portion of the
    # other (car partially exited the frame, left characters were cut by the edge).
    # Require at least 5 visible chars and at most 4 chars missing from the left.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 5 and len(longer) - len(shorter) <= 4 and longer.endswith(shorter):
        return True
    if abs(len(a) - len(b)) > 2:
        return False
    da, db = plate_digits(a), plate_digits(b)
    if len(da) >= 5 and len(db) >= 5 and da[-5:] == db[-5:]:
        return True
    if len(a) >= 6 and len(b) >= 6 and a[:4] == b[:4]:
        return _edit_distance(a, b) <= 3
    return _edit_distance(a, b) <= 2


def _prune_clusters(now_utc: datetime.datetime) -> None:
    ttl = datetime.timedelta(seconds=plate_cluster_ttl_seconds())
    while _cluster_canonicals and now_utc - _cluster_canonicals[0][1] > ttl:
        _cluster_canonicals.popleft()


def canonical_plate(norm: str, now_utc: datetime.datetime | None = None) -> str:
    """
    Map a normalized plate read to a cluster representative string.
    Similar reads within TTL share one canonical key for cooldown / dedup.
    """
    if not norm:
        return norm
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _prune_clusters(now)
        for rep, _seen in reversed(_cluster_canonicals):
            if plates_similar(rep, norm):
                return rep
        _cluster_canonicals.append((norm, now))
        return norm


def reset_plate_clusters() -> None:
    """Clear in-memory clusters (tests)."""
    with _lock:
        _cluster_canonicals.clear()
