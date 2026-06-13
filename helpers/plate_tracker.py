"""IoU plate tracker — detect every frame, OCR only on new/stale tracks."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Literal

from helpers.plate_cluster import plates_similar


def plate_tracking_enabled() -> bool:
    return os.environ.get("PLATE_TRACKING_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def plate_detect_fps() -> float:
    return max(1.0, min(30.0, float(os.environ.get("PLATE_DETECT_FPS", "10"))))


def plate_track_max_age_seconds() -> float:
    return max(0.3, float(os.environ.get("PLATE_TRACK_MAX_AGE_SECONDS", "4.0")))


def plate_track_ocr_max_attempts() -> int:
    return max(1, int(os.environ.get("PLATE_TRACK_OCR_MAX_ATTEMPTS", "4")))


def plate_track_ocr_interval_seconds() -> float:
    return max(0.1, float(os.environ.get("PLATE_TRACK_OCR_INTERVAL_SECONDS", "0.35")))


def plate_track_iou_threshold() -> float:
    return max(0.05, min(0.95, float(os.environ.get("PLATE_TRACK_IOU_THRESHOLD", "0.3"))))


def plate_track_min_hits() -> int:
    """Detect frames a box must appear on before OCR (filters one-frame noise)."""
    return max(1, int(os.environ.get("PLATE_TRACK_MIN_HITS", "1")))


def plate_track_vote_count() -> int:
    """Matching OCR reads on the same track before logging."""
    return max(1, int(os.environ.get("PLATE_TRACK_VOTE_COUNT", "2")))


def plate_track_instant_log_confidence() -> float:
    """One OCR read at or above this confidence logs immediately (no vote wait)."""
    return max(0.5, min(1.0, float(os.environ.get("PLATE_TRACK_INSTANT_LOG_CONF", "0.82"))))


def plate_track_single_log_confidence() -> float:
    """On final OCR attempt, allow a single read at or above this confidence."""
    return max(0.45, min(1.0, float(os.environ.get("PLATE_TRACK_SINGLE_LOG_CONF", "0.72"))))


def plate_track_uncertain_conf_min() -> float:
    """Minimum combined confidence for an uncertain audit log."""
    return max(0.0, min(1.0, float(os.environ.get("PLATE_TRACK_UNCERTAIN_CONF_MIN", "0.55"))))


def plate_track_uncertain_conf_max() -> float:
    """Exclusive upper bound — reads at/above instant log stay confirmed only."""
    raw = os.environ.get("PLATE_TRACK_UNCERTAIN_CONF_MAX", "").strip()
    if raw:
        return max(0.0, min(1.0, float(raw)))
    return plate_track_instant_log_confidence()


def plate_track_confirmed_min_confidence() -> float:
    """Multi-vote reads below this log as uncertain instead of unregistered."""
    return max(0.0, min(1.0, float(os.environ.get("PLATE_TRACK_CONFIRMED_MIN_CONF", "0.70"))))


def plate_track_nms_iou() -> float:
    return max(0.1, min(0.95, float(os.environ.get("PLATE_TRACK_NMS_IOU", "0.45"))))


def _box_to_xyxy(box: dict) -> tuple[float, float, float, float]:
    x = float(box.get("x", 0))
    y = float(box.get("y", 0))
    w = float(box.get("w", 0))
    h = float(box.get("h", 0))
    return x, y, x + w, y + h


def box_iou(a: dict, b: dict) -> float:
    ax1, ay1, ax2, ay2 = _box_to_xyxy(a)
    bx1, by1, bx2, by2 = _box_to_xyxy(b)
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def nms_detections(
    detections: list[dict],
    *,
    iou_threshold: float | None = None,
) -> list[dict]:
    """Drop overlapping plate boxes — keep highest confidence (one track per plate)."""
    if len(detections) <= 1:
        return detections
    threshold = plate_track_nms_iou() if iou_threshold is None else iou_threshold
    ordered = sorted(
        detections,
        key=lambda item: float(item.get("confidence") or 0),
        reverse=True,
    )
    kept: list[dict] = []
    for det in ordered:
        box = det.get("box")
        if not isinstance(box, dict):
            continue
        if any(box_iou(box, kept_item.get("box", {})) >= threshold for kept_item in kept):
            continue
        kept.append(det)
    return kept


@dataclass(frozen=True)
class TrackLogDecision:
    tier: Literal["confirmed", "uncertain"]
    read: dict
    reason: str


def _read_confidence(read: dict) -> float:
    return float(read.get("confidence") or 0)


def _best_ocr_read(reads: list[dict]) -> dict | None:
    if not reads:
        return None
    return max(reads, key=_read_confidence)


def _largest_vote_cluster(reads: list[dict]) -> tuple[list[dict], int]:
    """Return the largest fuzzy-similar OCR cluster and its size."""
    clusters: list[list[dict]] = []
    for read in reads:
        norm = str(read.get("plate_normalized") or "")
        if not norm:
            continue
        matched = False
        for cluster in clusters:
            rep = str(cluster[0].get("plate_normalized") or "")
            if plates_similar(rep, norm):
                cluster.append(read)
                matched = True
                break
        if not matched:
            clusters.append([read])

    if not clusters:
        return [], 0

    best = max(clusters, key=lambda cluster: (len(cluster), max(_read_confidence(r) for r in cluster)))
    return best, len(best)


@dataclass
class PlateTrack:
    track_id: int
    box: dict
    det_conf: float
    hits: int = 1
    last_seen: float = 0.0
    first_seen_at: float = 0.0
    ocr_attempts: int = 0
    last_ocr_at: float = 0.0
    ocr_pending: bool = False
    confirmed: bool = False
    logged: bool = False
    uncertain_logged: bool = False
    plate_text: str | None = None
    plate_normalized: str | None = None
    confidence: float = 0.0
    ocr_reads: list[dict] = field(default_factory=list)
    first_ocr_queued_at: float = 0.0


@dataclass
class PlateTracker:
    """Greedy IoU association (SORT-style) for plate boxes."""

    iou_threshold: float = field(default_factory=plate_track_iou_threshold)
    max_age_seconds: float = field(default_factory=plate_track_max_age_seconds)
    ocr_max_attempts: int = field(default_factory=plate_track_ocr_max_attempts)
    ocr_interval_seconds: float = field(default_factory=plate_track_ocr_interval_seconds)
    _next_id: int = 1
    _tracks: dict[int, PlateTrack] = field(default_factory=dict)

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1

    def active_tracks(self) -> list[PlateTrack]:
        return list(self._tracks.values())

    def get_track(self, track_id: int) -> PlateTrack | None:
        return self._tracks.get(track_id)

    def track_for_box(
        self,
        box: dict,
        *,
        iou_threshold: float | None = None,
    ) -> PlateTrack | None:
        """Find an active track whose last box overlaps `box` above the IoU gate."""
        threshold = self.iou_threshold if iou_threshold is None else iou_threshold
        best: tuple[float, PlateTrack] | None = None
        for track in self._tracks.values():
            iou = box_iou(track.box, box)
            if iou < threshold:
                continue
            if best is None or iou > best[0]:
                best = (iou, track)
        return best[1] if best else None

    def track_for_ocr_text(
        self,
        plate_normalized: str,
        *,
        exclude_id: int | None = None,
    ) -> PlateTrack | None:
        """
        Find an active (unlogged) track whose recent OCR reads are similar to
        the given plate text. Used as an OCR-similarity fallback when IoU
        association fails because the car moved too far between scans.

        Returns the track whose best-matching read has the highest confidence.
        """
        if not plate_normalized:
            return None
        best_track: PlateTrack | None = None
        best_conf: float = -1.0
        for track in self._tracks.values():
            if track.logged:
                continue
            if exclude_id is not None and track.track_id == exclude_id:
                continue
            for read in reversed(track.ocr_reads):
                other_norm = str(read.get("plate_normalized") or "")
                if not other_norm:
                    continue
                if plates_similar(other_norm, plate_normalized):
                    conf = float(read.get("confidence") or 0)
                    if conf > best_conf:
                        best_track = track
                        best_conf = conf
                    break
        return best_track

    def merge_into(self, src_id: int, dst_id: int) -> bool:
        """
        Merge `src` track's reads, position, and hit count into `dst` and delete
        `src`. Used by the camera worker when OCR-similarity reveals two tracks
        are actually the same car (e.g. IoU missed because the car moved a lot).

        Returns True if a merge happened.
        """
        if src_id == dst_id:
            return False
        src = self._tracks.get(src_id)
        dst = self._tracks.get(dst_id)
        if src is None or dst is None:
            return False
        # Carry the freshest position info from src — it represents the latest frame.
        if src.last_seen >= dst.last_seen:
            dst.box = dict(src.box)
            dst.last_seen = src.last_seen
        dst.det_conf = max(dst.det_conf, src.det_conf)
        dst.hits += max(1, src.hits)
        dst.ocr_attempts += src.ocr_attempts
        if src.first_ocr_queued_at > 0 and (
            dst.first_ocr_queued_at == 0 or src.first_ocr_queued_at < dst.first_ocr_queued_at
        ):
            dst.first_ocr_queued_at = src.first_ocr_queued_at
        if src.first_seen_at > 0 and (
            dst.first_seen_at == 0 or src.first_seen_at < dst.first_seen_at
        ):
            dst.first_seen_at = src.first_seen_at
        for read in src.ocr_reads:
            dst.ocr_reads.append(dict(read))
        del self._tracks[src_id]
        return True

    def update(
        self, detections: list[dict], *, now: float
    ) -> tuple[list[PlateTrack], list[PlateTrack]]:
        """
        Match detections to tracks, prune stale tracks.
        Returns (tracks needing OCR, expired tracks removed this tick).
        """
        det_boxes = nms_detections([d for d in detections if isinstance(d.get("box"), dict)])
        track_ids = list(self._tracks.keys())
        matched_tracks: set[int] = set()
        matched_dets: set[int] = set()
        pairs: list[tuple[float, int, int]] = []

        for ti, tid in enumerate(track_ids):
            for di, det in enumerate(det_boxes):
                iou = box_iou(self._tracks[tid].box, det["box"])
                if iou >= self.iou_threshold:
                    pairs.append((iou, ti, di))

        pairs.sort(key=lambda item: item[0], reverse=True)
        track_match: dict[int, int] = {}
        det_match: dict[int, int] = {}
        for _iou, ti, di in pairs:
            tid = track_ids[ti]
            if tid in track_match or di in det_match:
                continue
            track_match[tid] = di
            det_match[di] = tid
            matched_tracks.add(tid)
            matched_dets.add(di)

        for tid, di in track_match.items():
            det = det_boxes[di]
            track = self._tracks[tid]
            track.box = dict(det["box"])
            track.det_conf = max(track.det_conf, float(det.get("confidence") or 0))
            track.hits += 1
            track.last_seen = now

        for di, det in enumerate(det_boxes):
            if di in matched_dets:
                continue
            tid = self._next_id
            self._next_id += 1
            self._tracks[tid] = PlateTrack(
                track_id=tid,
                box=dict(det["box"]),
                det_conf=float(det.get("confidence") or 0),
                last_seen=now,
                first_seen_at=now,
            )

        stale = [
            tid
            for tid, track in self._tracks.items()
            if now - track.last_seen > self.max_age_seconds
        ]
        expired = [self._tracks[tid] for tid in stale]
        for tid in stale:
            self._tracks.pop(tid, None)

        need_ocr = [track for track in self._tracks.values() if self._needs_ocr(track, now=now)]
        return need_ocr, expired

    def _needs_ocr(self, track: PlateTrack, *, now: float) -> bool:
        if track.logged or track.ocr_pending:
            return False
        if track.hits < plate_track_min_hits():
            return False
        if track.ocr_attempts >= self.ocr_max_attempts:
            return False
        if track.ocr_attempts == 0:
            return True
        return (now - track.last_ocr_at) >= self.ocr_interval_seconds

    def record_ocr_read(self, track_id: int, read: dict) -> None:
        track = self._tracks.get(track_id)
        if track is None or not isinstance(read, dict):
            return
        track.ocr_reads.append(dict(read))

    def resolve_track_log(
        self,
        track: PlateTrack,
        *,
        on_expiry: bool = False,
    ) -> TrackLogDecision | None:
        """
        Decide whether a track should log as confirmed or uncertain.

        Confirmed (live): instant conf, multi-vote with max conf >= confirmed_min,
        or final single read >= single_conf when that read is also >= confirmed_min.

        Uncertain (expiry only): vote not met and best read in uncertain band, or
        multi-vote met with max conf < confirmed_min when the track is dropped.
        """
        if track.logged or track.uncertain_logged or not track.ocr_reads:
            return None

        vote_count = plate_track_vote_count()
        instant_conf = plate_track_instant_log_confidence()
        single_conf = plate_track_single_log_confidence()
        uncertain_min = plate_track_uncertain_conf_min()
        uncertain_max = plate_track_uncertain_conf_max()
        confirmed_min = plate_track_confirmed_min_confidence()

        best_read = _best_ocr_read(track.ocr_reads)
        if best_read is None:
            return None
        best_conf = _read_confidence(best_read)

        cluster, cluster_size = _largest_vote_cluster(track.ocr_reads)
        cluster_best = _best_ocr_read(cluster) if cluster else best_read
        cluster_conf = _read_confidence(cluster_best) if cluster_best else 0.0

        if best_conf >= instant_conf:
            return TrackLogDecision(tier="confirmed", read=best_read, reason="instant_log")

        if cluster_size >= vote_count and cluster_conf >= confirmed_min:
            return TrackLogDecision(
                tier="confirmed",
                read=cluster_best or best_read,
                reason="vote_confirmed",
            )

        if not on_expiry:
            if (
                track.ocr_attempts >= self.ocr_max_attempts
                and best_conf >= single_conf
                and best_conf >= confirmed_min
            ):
                return TrackLogDecision(tier="confirmed", read=best_read, reason="single_confirmed")
            return None

        # Track expired — audit weak reads that never confirmed.
        if (
            cluster_size >= vote_count
            and uncertain_min <= cluster_conf < confirmed_min
        ):
            return TrackLogDecision(
                tier="uncertain",
                read=cluster_best or best_read,
                reason="expired_vote_uncertain",
            )

        if cluster_size < vote_count and uncertain_min <= best_conf < uncertain_max:
            return TrackLogDecision(
                tier="uncertain",
                read=best_read,
                reason="expired_single_uncertain",
            )

        return None

    def stable_read_for_logging(self, track_id: int) -> dict | None:
        """Return best read once confirmed track-level gates pass."""
        track = self._tracks.get(track_id)
        if track is None:
            return None
        decision = self.resolve_track_log(track, on_expiry=False)
        if decision is None or decision.tier != "confirmed":
            return None
        return decision.read

    def mark_ocr_pending(self, track_id: int, *, now: float) -> None:
        track = self._tracks.get(track_id)
        if track is None:
            return
        track.ocr_pending = True
        track.ocr_attempts += 1
        track.last_ocr_at = now
        if track.first_ocr_queued_at <= 0:
            track.first_ocr_queued_at = now

    def mark_ocr_finished(self, track_id: int) -> None:
        track = self._tracks.get(track_id)
        if track is None:
            return
        track.ocr_pending = False

    def mark_ocr_started(self, track_id: int, *, now: float) -> None:
        """Backward-compatible alias for mark_ocr_pending."""
        self.mark_ocr_pending(track_id, now=now)

    def mark_confirmed(
        self,
        track_id: int,
        *,
        plate_text: str,
        plate_normalized: str,
        confidence: float,
        logged: bool,
    ) -> None:
        track = self._tracks.get(track_id)
        if track is None or not logged:
            return
        track.confirmed = True
        track.plate_text = plate_text
        track.plate_normalized = plate_normalized
        track.confidence = confidence
        track.logged = True

    def mark_uncertain_logged(self, track_id: int) -> None:
        track = self._tracks.get(track_id)
        if track is None:
            return
        track.uncertain_logged = True
        track.logged = True

    def log_timing_for_track(self, track: PlateTrack, *, now: float | None = None) -> dict:
        """Timing stats for a track object (including expired tracks)."""
        end = now if now is not None else time.monotonic()
        started = track.first_seen_at or track.last_seen
        wrap_s = round(max(0.0, end - started), 3)
        timing = {
            "track_id": track.track_id,
            "wrap_s": wrap_s,
            "detect_to_log_s": wrap_s,
            "ocr_attempts": track.ocr_attempts,
            "track_hits": track.hits,
        }
        if track.first_ocr_queued_at > 0:
            timing["ocr_elapsed_s"] = round(max(0.0, end - track.first_ocr_queued_at), 3)
        return timing

    def log_timing(self, track_id: int, *, now: float | None = None) -> dict:
        """Seconds from first detection to now, plus track OCR stats."""
        track = self._tracks.get(track_id)
        if track is None:
            return {}
        return self.log_timing_for_track(track, now=now)
