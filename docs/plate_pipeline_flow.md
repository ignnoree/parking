# Frame capture to parking log

End-to-end flow from app startup until a vehicle is written to `parking_logs`. For worker config and troubleshooting see [camera_worker.md](camera_worker.md).

---

## Overview

```
main.py
  └─ start_camera_worker_thread()
       workers/camera_worker.py
         ├─ drainer thread (per camera)     → reads video, enqueues scan jobs
         ├─ detect thread                   → OCR + tracker + log
         ├─ preview thread                  → MJPEG live stream
         └─ helpers/plate_detect_isolated.py (child process) → YOLO + OCR
              └─ helpers/plate_worker_pipeline.py → helpers/plate_pipeline.py
```

**Two processes:** Flask + camera threads run in the **main process**. Heavy ML runs in a **child process** (`plate-ocr-worker`) so inference does not block the web server or camera capture.

**Tracker mode (default):** OCR returns reads only; the main process votes across frames, then logs. **Legacy mode** (`PLATE_TRACKING_ENABLED=false`): the child logs directly on each scan.

---

## 1. Startup

`main.py` starts background workers when the app boots:

```python
# main.py
start_camera_worker_thread()
```

`start_camera_worker_thread()` (`workers/camera_worker.py` ~802) loads enabled cameras from PostgreSQL via `load_camera_configs()` and calls `_start_runtime()`.

`_start_runtime()` creates `_WorkerRuntime` in memory:

| Object | Purpose |
|--------|---------|
| `detect_queue` | Thread-safe queue of `_DetectJob` (max size `CAMERA_DETECT_QUEUE_SIZE`, default 4) |
| `drainer_threads` | One thread per camera — reads RTSP/USB/file |
| `detect_thread` | Consumes queue, runs `_run_plate_detect_on_frame` |
| `preview_thread` | Encodes `_latest_frame` → MJPEG |
| `track_states[camera_id]` | Per-camera `PlateTracker` for multi-frame voting |

The OCR child process starts **lazily** on the first scan (`ensure_started()` in `helpers/plate_detect_isolated.py`).

---

## 2. Frame capture and job creation

Each **drainer thread** (`_drainer_loop`, `workers/camera_worker.py` ~574) opens the camera with OpenCV and reads frames in a loop.

- Every frame → primary camera updates `_latest_frame` for live preview.
- Every `frame_interval_seconds` → one frame is queued for plate detection.

```652:660:workers/camera_worker.py
            _enqueue_detect(
                _DetectJob(
                    frame=frame.copy(),
                    camera_id=config.id,
                    direction=config.gate_role,
                    light_profile=config.light_profile,
                ),
                detect_queue,
            )
```

**`_DetectJob`** holds a **copy** of the frame (numpy array in RAM) plus `camera_id`, `direction` (entry/exit), and `light_profile`.

`_enqueue_detect()` puts the job on `detect_queue`. If the queue is full, it drops the **oldest** job so newer frames are still processed.

The **detect thread** (`_detect_worker_loop` ~539) blocks on `detect_queue.get()` and calls `_run_plate_detect_on_frame(job)`.

---

## 3. Save frame and run OCR

`_run_plate_detect_on_frame` (`workers/camera_worker.py` ~474):

1. Writes `job.frame` to `uploads/temp/camera_{id}_{uuid}.jpg`
2. Calls `detect_with_retries(detect_frame_isolated, ...)` (`helpers/plate_detect_retries.py`)

`detect_with_retries` retries up to `PLATE_DETECT_MAX_RETRIES + 1` times if the child returns `None`.

When tracking is enabled, `skip_logging=True` is passed so the child **does not** write to the parking log yet:

```489:496:workers/camera_worker.py
        result = detect_with_retries(
            detect_frame_isolated,
            path,
            direction=job.direction,
            light_profile=profile,
            frame_shape=job.frame.shape[:2],
            skip_logging=tracker_on,
        )
```

---

## 4. Isolated OCR child process

`detect_frame_isolated` (`helpers/plate_detect_isolated.py` ~126) is the **client**. `_worker_loop` (~23) is the **server** in a separate OS process.

```
main process (detect thread)          child process (plate-ocr-worker)
  detect_frame_isolated()               _worker_loop()  [infinite loop]
       │                                      │
       │  _request_q.put(tuple)               │  request_q.get()
       │  (path, direction, skip_logging…)    │  open JPEG from disk
       │                                      │  run_plate_detect_on_file_obj()
       │  _response_q.get()  ◄─────────────── │  response_q.put({result: ...})
       │                                      │
  returns result dict                   YOLO + OCR models in child RAM
```

- `_detect_call_lock` — only one OCR job at a time from the main process.
- The numpy frame is **not** sent over IPC; the child reads the JPEG path from disk.
- If `skip_logging=True`, the child leaves the temp file on disk for the tracker to use when logging.

`run_plate_detect_on_file_obj` (`helpers/plate_worker_pipeline.py`) calls `run_plate_detect_on_file` (`helpers/plate_pipeline.py`):

1. YOLO finds plate boxes (`get_alpr().predict`)
2. OCR reads text, normalizes plate, scores format
3. Looks up vehicle in PostgreSQL (`find_vehicle_by_normalized`)
4. Drops reads below `PLATE_OCR_MIN_CONFIDENCE` (default 0.42)

Returns a dict:

```python
{
  "status": "ok",
  "plates_detected": 1,
  "results": [
    {"plate_text": "...", "plate_normalized": "...", "confidence": 0.78,
     "box": {"x", "y", "w", "h"}, "match_status": "registered", ...}
  ]
}
```

---

## 5. Lighting monitor (not voting, not overlay)

After OCR, if tracker mode is on:

```502:502:workers/camera_worker.py
            note_plate_scan(light_profile=profile, plates_logged=plates_detected)
```

`note_plate_scan` (`helpers/lighting_monitor.py`) only increments a global `_empty_streak` counter when scans find zero plates under `high_glare` or `low_light`. After 15 empty scans it writes a `lighting_warning` to `software_logs`. It does **not** affect logging or the live overlay.

---

## 6. Multi-frame tracker and voting

`_route_results_through_tracker` (`workers/camera_worker.py` ~272) runs in the **main process** on the detect thread.

### 6a. Associate boxes with tracks

```371:374:workers/camera_worker.py
    _need_ocr, expired_tracks = tracker.update(
        [{"box": r.get("box"), "confidence": r.get("confidence")} for r in raw_results if r.get("box")],
        now=now,
    )
```

`PlateTracker.update` (`helpers/plate_tracker.py` ~316):

- NMS on overlapping detections
- Match boxes to existing tracks by IoU (`PLATE_TRACK_IOU_THRESHOLD`)
- Create new tracks for new cars; increment `hits` on re-detection
- Remove stale tracks after `PLATE_TRACK_MAX_AGE_SECONDS` (default 4s)

Tracker state lives in `_runtime.track_states[camera_id].tracker._tracks` — a `dict[track_id → PlateTrack]` that **persists across frames**.

### 6b. Record OCR reads

For each detection matched to a track:

```428:438:workers/camera_worker.py
        tracker.record_ocr_read(track.track_id, {
            "plate_text": det.get("plate_text"),
            "plate_normalized": det.get("plate_normalized"),
            "confidence": det.get("confidence"),
            ...
        })
```

Appends to `track.ocr_reads` in RAM. Skips if `track.logged` or `hits < PLATE_TRACK_MIN_HITS`.

### 6c. Vote — `resolve_track_log`

```455:455:workers/camera_worker.py
        decision = tracker.resolve_track_log(live_track, on_expiry=False)
```

Voting logic (`helpers/plate_tracker.py` ~397):

1. `_largest_vote_cluster(track.ocr_reads)` — groups similar plate texts (`plates_similar`)
2. Compare cluster size to `PLATE_TRACK_VOTE_COUNT` (default 2)

| Outcome | Condition |
|---------|-----------|
| **Confirmed** (`instant_log`) | Best read conf ≥ `PLATE_TRACK_INSTANT_LOG_CONF` (0.82) |
| **Confirmed** (`vote_confirmed`) | ≥ vote_count similar reads AND max conf ≥ `PLATE_TRACK_CONFIRMED_MIN_CONF` (0.70) |
| **Confirmed** (`single_confirmed`) | Last OCR attempt, single read ≥ 0.72 and ≥ 0.70 |
| **Wait** | Returns `None` — car needs more scan frames |

When a track **expires** (car left frame), `resolve_track_log(..., on_expiry=True)` may log an **uncertain** audit record instead of a confirmed entry/exit.

### 6d. Persist confirmed read

When `decision.tier == "confirmed"`, `_process_live_decision` (~318):

1. `build_result_row()` — shape row for logging
2. `log_parking_events_for_results(frame_path, payload)` — DB write (see §7)
3. `tracker.mark_confirmed(..., logged=True)` — prevents duplicate logs for same car

Returns `overlay_payload` — only plates logged **this frame**:

```465:471:workers/camera_worker.py
    return {
        "status": "ok",
        "plates_detected": len(logged_payload),
        "results": logged_payload,
        "logged_plates": logged_payload,
    }
```

---

## 7. Parking log persistence

`log_parking_events_for_results` (`helpers/parking_logging.py`) applies final gates:

| Gate | Purpose |
|------|---------|
| Jitter cooldown | Block similar plate re-logs within `PARKING_JITTER_COOLDOWN_SECONDS` |
| Parking cooldown | DB setting — min seconds between same plate + direction |
| Stability (legacy only) | When tracking off — require N agreeing reads before unregistered log |

On success:

1. Source frame → `uploads/known_parking_logs/sources/` or `unknown_parking_logs/sources/`
2. Plate crop → matching `crops/` folder
3. `log_parking_event()` → PostgreSQL `parking_logs` row (`database/logs_db.py`)
4. Console: `[PARKING_LOGGED] plate=... direction=entry ...`

In-memory cooldown maps (`_last_parking_log_at`, `_read_history`) update under a module lock.

---

## 8. Live overlay

Only the **primary camera** flashes boxes on the MJPEG stream:

```510:511:workers/camera_worker.py
            if overlay_payload and job.camera_id == _runtime.primary_camera_id:
                update_overlay_from_plate_results(job.frame.shape, overlay_payload)
```

`update_overlay_from_plate_results` (`helpers/live_frame_buffer.py` ~74) reads `logged_plates` and appends to global `_logged_flashes`. The preview thread draws colored rectangles for ~`LIVE_LOG_OVERLAY_SECONDS` (default 2s):

- Green — resident
- Cyan — guest
- Red — unregistered
- Orange — uncertain

---

## 9. Cleanup

If tracker mode was on, the temp JPEG is deleted in the `finally` block of `_run_plate_detect_on_frame` (~518). `job.frame` is freed when the detect thread finishes the job.

---

## Legacy path (tracking disabled)

When `PLATE_TRACKING_ENABLED=false`:

- `skip_logging=False` → child calls `log_parking_events_for_results` directly in `plate_worker_pipeline.py`
- Main process skips `_route_results_through_tracker`
- Overlay uses `result["logged_plates"]` from the child

---

## Key files (read in order)

1. `main.py` — `start_camera_worker_thread()`
2. `workers/camera_worker.py` — `_drainer_loop`, `_enqueue_detect`, `_run_plate_detect_on_frame`, `_route_results_through_tracker`
3. `helpers/plate_detect_isolated.py` — `detect_frame_isolated`, `_worker_loop`
4. `helpers/plate_pipeline.py` — `run_plate_detect_on_file`
5. `helpers/plate_tracker.py` — `PlateTracker.update`, `resolve_track_log`, `_largest_vote_cluster`
6. `helpers/parking_logging.py` — `log_parking_events_for_results`
7. `database/logs_db.py` — `log_parking_event`

---

## Key env vars

| Variable | Default | Effect |
|----------|---------|--------|
| `PLATE_TRACKING_ENABLED` | `true` | Vote across frames before log |
| `PLATE_TRACK_VOTE_COUNT` | `2` | Similar reads required to confirm |
| `PLATE_TRACK_INSTANT_LOG_CONF` | `0.82` | Skip vote on very high confidence |
| `PLATE_TRACK_MIN_HITS` | `1` | Detection frames before OCR counts |
| `PLATE_OCR_MIN_CONFIDENCE` | `0.42` | Per-frame confidence gate |
| `CAMERA_DETECT_QUEUE_SIZE` | `4` | Max queued scan jobs |
| `PLATE_DETECT_MAX_RETRIES` | `2` | OCR retry count |

Full list: `.env.example`

---

## Related tests

```bash
python -m pytest tests/test_plate_tracker.py tests/test_parking_logging.py -q
```
