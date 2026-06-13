# Plate pipeline: capture to parking log

This document explains what happens from the moment the camera captures a frame until a car is written to the parking log. Each step references the source files and functions involved.

For worker configuration and troubleshooting, see [camera_worker.md](camera_worker.md). For admin UI and camera setup, see [admin_guide.md](admin_guide.md).

---

## Summary

1. The app starts background workers when it boots.
2. A **drainer thread** reads video from each enabled camera continuously.
3. On a timer (`frame_interval_seconds`), one frame is queued for plate detection.
4. A **detect thread** saves the frame and sends it to an **isolated OCR child process**.
5. **YOLO** finds plate boxes; **OCR** reads text; **format filters** reject garbage; the **vehicle DB** is checked.
6. With tracking enabled (default), a **per-camera tracker** votes across multiple frames before logging.
7. **Parking logging** applies cooldowns, saves snapshot images, writes PostgreSQL, and prints `[PARKING_LOGGED]` with timing.

---

## Architecture

```mermaid
flowchart TB
    subgraph startup [App startup]
        M[main.py] --> CW[start_camera_worker_thread]
    end

    subgraph capture [Per camera — workers/camera_worker.py]
        D[Drainer thread] -->|every frame| PV[Preview thread → live UI]
        D -->|every N seconds| Q[Detect queue]
    end

    subgraph detect [Detect thread]
        Q --> JPG[Save temp JPEG]
        JPG --> ISO[plate_detect_isolated.py — child process]
    end

    subgraph ocr [OCR child — helpers/plate_worker_pipeline.py]
        ISO --> PP[plate_pipeline.py]
        PP --> YOLO[YOLO detect plate box]
        YOLO --> OCR[OCR read text]
        OCR --> FMT[plate_format.py validate]
        FMT --> VDB[vehicles_db lookup]
    end

    subgraph track [Tracker mode — default]
        VDB --> TR[plate_tracker.py vote]
        TR -->|confirmed| PL[parking_logging.py]
    end

    subgraph legacy [Legacy mode — PLATE_TRACKING_ENABLED=false]
        VDB --> PL2[parking_logging.py direct]
    end

    PL --> DB[(PostgreSQL parking_logs)]
    PL --> IMG[uploads/known|unknown_parking_logs/]
```

---

## File map

| Role | File | Main entry points |
|------|------|-------------------|
| App bootstrap | `main.py` | `start_background_workers()` |
| Camera + threads | `workers/camera_worker.py` | `_drainer_loop`, `_detect_worker_loop`, `_run_plate_detect_on_frame`, `_route_results_through_tracker` |
| Isolated OCR process | `helpers/plate_detect_isolated.py` | `detect_frame_isolated`, `_worker_loop` |
| Detect wrapper | `helpers/plate_worker_pipeline.py` | `run_plate_detect_on_file_obj` |
| YOLO + OCR per frame | `helpers/plate_pipeline.py` | `detect_plates_in_image`, `run_plate_detect_on_file`, `build_result_row` |
| ALPR engine | `helpers/plate_alpr.py` | `get_alpr`, OCR backends |
| Format / normalize | `helpers/plate_format.py`, `helpers/plate_normalize.py` | `is_plausible_plate`, `normalize_plate` |
| Multi-frame voting | `helpers/plate_tracker.py` | `PlateTracker.update`, `stable_read_for_logging`, `log_timing` |
| Log persistence | `helpers/parking_logging.py` | `log_parking_events_for_results` |
| DB write | `database/logs_db.py` | `log_parking_event` |
| Vehicle lookup | `database/vehicles_db.py` | `find_vehicle_by_normalized` |
| Live overlay | `helpers/live_frame_buffer.py` | `update_overlay_from_plate_results`, `publish_frame` |
| Snapshot cleanup | `helpers/snapshot_retention.py` | background purge of old crop/source files |

---

## Step 1 — Application startup

When the Flask app starts, it launches guest expiry and the camera worker:

```python
# main.py
def start_background_workers() -> None:
    purge_expired_guest_vehicles()
    start_guest_expiry_thread()
    start_camera_worker_thread()
```

The OCR child process is **not** started here; it starts lazily on the first plate scan (`ensure_started()` in `helpers/plate_detect_isolated.py`).

---

## Step 2 — Load cameras and start threads

`start_camera_worker_thread()` loads enabled cameras from PostgreSQL and calls `_start_runtime()`:

```python
# workers/camera_worker.py — load_camera_configs()
for row in list_cameras(enabled_only=True):
    cfg = _config_from_row(row)
```

`_start_runtime()` starts three kinds of threads:

| Thread | Function | Purpose |
|--------|----------|---------|
| Preview | `_preview_publisher_loop` | MJPEG / live view (`helpers/live_frame_buffer.py`) |
| Detect | `_detect_worker_loop` | Processes queued scan frames |
| Drainer (×N cameras) | `_drainer_loop` | Reads video; enqueues scans |

Per-camera fields from the DB: `source`, `gate_role` (entry/exit), `light_profile`, `frame_interval_seconds`.

---

## Step 3 — Capture frames (drainer)

The drainer opens the camera with OpenCV (`_open_capture`) and reads frames in a loop.

For RTSP/HTTP streams, `_read_drainer_burst` reads multiple frames and keeps the **latest** to reduce latency from buffered video:

```python
# workers/camera_worker.py — _read_drainer_burst()
for _ in range(_buffer_drain_reads()):
    ok, frame = cap.read()
    if ok and frame is not None:
        latest_ok, latest = True, frame
```

Every frame updates the live preview on the primary camera (`_set_latest_frame`). Plate scans are **not** every frame — only when the interval elapses:

```python
# workers/camera_worker.py — _drainer_loop()
if now - last_detect >= config.frame_interval_seconds:
    last_detect = now
    _enqueue_detect(_DetectJob(
        frame=frame.copy(),
        camera_id=config.id,
        direction=config.gate_role,
        light_profile=config.light_profile,
    ), detect_queue)
```

The detect queue drops the oldest job when full so scans keep up under load (`_enqueue_detect`).

---

## Step 4 — Save snapshot and submit to OCR

The detect thread dequeues jobs and runs `_run_plate_detect_on_frame`:

1. Writes `uploads/temp/camera_{id}_{uuid}.jpg`
2. Resolves the camera light profile (`helpers/light_profile.py`)
3. Calls `detect_with_retries(detect_frame_isolated, ...)` (`helpers/plate_detect_retries.py`)

When **tracking is enabled** (default), logging is deferred:

```python
# workers/camera_worker.py — _run_plate_detect_on_frame()
tracker_on = plate_tracking_enabled() and _runtime is not None
result = detect_with_retries(
    detect_frame_isolated,
    path,
    ...
    skip_logging=tracker_on,
)
```

`skip_logging=True` tells the child process to return OCR results **without** writing a parking log yet.

---

## Step 5 — Isolated OCR child process

Plate AI runs in a separate process so heavy model inference does not block the camera or web server:

```python
# helpers/plate_detect_isolated.py — _worker_loop()
get_alpr()  # models load once per child PID
result = run_plate_detect_on_file_obj(
    f, path, direction=..., light_profile=..., skip_logging=skip_logging,
)
```

The parent sends the job via multiprocessing queues and waits on `_response_q` (`detect_frame_isolated`). A global lock ensures one OCR job at a time (`_detect_call_lock`).

---

## Step 6 — YOLO detection + OCR on one frame

`run_plate_detect_on_file_obj` (`helpers/plate_worker_pipeline.py`) wraps the core pipeline:

```python
with light_profile_scope(effective_profile):
    result = run_plate_detect_on_file(frame_path, direction=gate)
```

`run_plate_detect_on_file` (`helpers/plate_pipeline.py`):

1. **`detect_plates_in_image`** — calls `get_alpr().predict(image_path)` (YOLO + OCR ensemble in `helpers/plate_alpr.py`)
2. For each raw detection:
   - Cleans OCR text (`clean_plate_ocr_text`)
   - Scores format (`plate_format_score`, `is_plausible_plate`)
   - Computes combined confidence (det × OCR × format)
   - Normalizes plate (`normalize_plate`)
3. **`_lookup_vehicle`** — registered / guest / expired guest handling
4. Returns payload:

```python
{
    "status": "ok",
    "direction": "entry" | "exit",
    "plates_detected": N,
    "results": [
        {
            "plate_text": "...",
            "plate_normalized": "...",
            "confidence": 0.75,
            "box": {"x", "y", "w", "h"},
            "match_status": "registered" | "unregistered",
            "vehicle_id": int | None,
            ...
        }
    ],
}
```

Plates below `PLATE_OCR_MIN_CONFIDENCE` (default `0.42`, see `plate_ocr_min_confidence()` in `plate_pipeline.py`) are dropped before results are returned.

---

## Step 7 — Multi-frame tracker (default path)

When `PLATE_TRACKING_ENABLED=true`, `_route_results_through_tracker` in `workers/camera_worker.py` processes OCR results before logging.

### 7a. Associate detections with tracks

```python
tracker.update(
    [{"box": r.get("box"), "confidence": r.get("confidence")} for r in raw_results],
    now=now,
)
```

`PlateTracker.update` (`helpers/plate_tracker.py`):

- Runs NMS on overlapping boxes (`nms_detections`)
- Matches boxes to existing tracks by IoU (`PLATE_TRACK_IOU_THRESHOLD`)
- Creates new tracks with `first_seen_at` for timing
- Drops stale tracks after `PLATE_TRACK_MAX_AGE_SECONDS`

### 7b. Record OCR reads per track

For each detection matched to a track:

- Skips if already logged or `hits < PLATE_TRACK_MIN_HITS`
- Optionally **merges** tracks when IoU fails but OCR text is similar (`track_for_ocr_text`, `merge_into`)
- Appends read to `track.ocr_reads` via `record_ocr_read`

### 7c. Vote before logging

`stable_read_for_logging(track_id)` returns a read only when:

| Rule | Env var (default) |
|------|-------------------|
| Instant log — one high-confidence read | `PLATE_TRACK_INSTANT_LOG_CONF` (0.82) |
| Vote — N agreeing reads (exact or similar) | `PLATE_TRACK_VOTE_COUNT` (2) |
| Last attempt — single read above threshold | `PLATE_TRACK_SINGLE_LOG_CONF` (0.72) |

If no stable read yet, the car waits for the next scan frame.

### 7d. Build log row and persist

When stable:

```python
timing = tracker.log_timing(track.track_id, now=now)
row = build_result_row(..., timing=timing, track_confirmed=True)
logged_plates = log_parking_events_for_results(frame_path, payload)
tracker.mark_confirmed(..., logged=True)
```

`track_confirmed=True` skips the legacy stability gate in `parking_logging.py` (tracker already voted).

The live overlay on the primary camera only shows **confirmed** plates (`update_overlay_from_plate_results`).

---

## Step 8 — Legacy path (tracking disabled)

If `PLATE_TRACKING_ENABLED=false`, the child process logs directly:

```python
# helpers/plate_worker_pipeline.py
logged_plates = log_parking_events_for_results(
    frame_path, result, wrap_started_at=wrap_started,
)
```

Unregistered plates then require **`PARKING_READ_STABILITY_COUNT`** agreeing reads within a time window before logging (`_stable_unregistered_read` in `parking_logging.py`).

---

## Step 9 — Parking log persistence

`log_parking_events_for_results` (`helpers/parking_logging.py`) runs final business rules:

| Gate | Purpose | Code |
|------|---------|------|
| Stability (legacy only) | Block single-frame OCR garbage | `_stable_unregistered_read` |
| Jitter cooldown | Block similar plate re-logs | `_jitter_cooldown_active` |
| Parking cooldown | DB-configured min seconds between same key | `parking_log_cooldown_seconds()` |

On success:

1. **Source frame** copied to `uploads/known_parking_logs/sources/` or `uploads/unknown_parking_logs/sources/`
2. **Plate crop** (annotated box) to matching `crops/` folder — `_persist_plate_crop` uses `helpers/plate_crop.py`
3. **PostgreSQL row** via `log_parking_event` (`database/logs_db.py` → `ParkingLog` model)
4. **Console log**:

```
[PARKING_LOGGED] plate=... direction=entry status=unregistered ... wrap_s=2.341s hits=3
```

Timing fields are stored in the event `details` JSON under `"timing"`.

Old snapshot files are purged by `helpers/snapshot_retention.py` (`SNAPSHOT_RETENTION_DAYS`, default 90).

---

## Timing metrics

| Field | Meaning | Source |
|-------|---------|--------|
| `detect_to_log_s` | Seconds from first track detection to log | `PlateTracker.log_timing()` — uses `first_seen_at` |
| `wrap_s` | Same as above when from tracker; otherwise seconds since OCR wrapper started | `_resolve_item_timing()` |
| `track_hits` | Detection frames the track appeared on | `log_timing()` |
| `ocr_attempts` | OCR attempts on the track | `log_timing()` |

Tracker timing is attached in `_route_results_through_tracker` before calling `log_parking_events_for_results`. Legacy mode uses `wrap_started_at` from `run_plate_detect_on_file_obj`.

---

## Key environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `PLATE_TRACKING_ENABLED` | `true` | Multi-frame vote before log |
| `PLATE_TRACK_VOTE_COUNT` | `2` | Agreeing reads required |
| `PLATE_TRACK_INSTANT_LOG_CONF` | `0.82` | Skip vote on very high confidence |
| `PLATE_TRACK_MIN_HITS` | `1` | Frames before OCR read counts |
| `PLATE_OCR_MIN_CONFIDENCE` | `0.42` | Per-frame confidence gate |
| `PARKING_READ_STABILITY_COUNT` | `2` | Legacy stability (tracking off) |
| `PARKING_JITTER_COOLDOWN_SECONDS` | `120` | Similar-plate re-log block |
| `PLATE_DEBUG` | off | Verbose tracker / format logs |

Full list: `.env.example` and [camera_worker.md](camera_worker.md).

---

## Demo walkthrough (files to open in order)

1. `main.py` — `start_background_workers()`
2. `workers/camera_worker.py` — `_start_runtime`, `_drainer_loop`, `_run_plate_detect_on_frame`, `_route_results_through_tracker`
3. `helpers/plate_detect_isolated.py` — `_worker_loop`
4. `helpers/plate_pipeline.py` — `detect_plates_in_image`, `run_plate_detect_on_file`
5. `helpers/plate_tracker.py` — `PlateTracker.update`, `stable_read_for_logging`
6. `helpers/parking_logging.py` — `log_parking_events_for_results`
7. `database/logs_db.py` — `log_parking_event`

---

## Related tests

| Test file | Covers |
|-----------|--------|
| `tests/test_plate_tracker.py` | IoU, NMS, voting, timing |
| `tests/test_parking_logging.py` | Stability, cooldown, wrap timing |
| `tests/test_plate_format.py` | Format validation |
| `tests/test_plate_alpr_langs.py` | OCR language paths |

Run: `python -m pytest tests/test_plate_tracker.py tests/test_parking_logging.py -q`
