# Camera worker

The server-side camera worker (`workers/camera_worker.py`) captures frames, runs plate OCR in an isolated child process, and persists parking logs.

For the full capture → OCR → tracker → log flow with code references, see [plate_pipeline_flow.md](plate_pipeline_flow.md).

## Architecture

```
Camera (RTSP / HTTP / USB / video file)
    → drainer thread (per camera)
    → temp JPEG in uploads/temp/
    → plate OCR child process (YOLO + PaddleOCR)
    → parking_logging → PostgreSQL + uploads/known|unknown_parking_logs/
```

Preview MJPEG uses a separate thread; plate detection does not block live view.

## Configuration

| Source | Notes |
|--------|--------|
| PostgreSQL `cameras` | Primary — manage via `/admin` or `/api/cameras` |
| `.env` `CAMERA_URL` / `CAMERA_URL_ENTRY` / `CAMERA_URL_EXIT` | Bootstrap only when `cameras` table is empty |

Per-camera fields: `gate_role` (entry/exit), `light_profile`, `frame_interval_seconds`, `is_enabled`.

Global settings (PostgreSQL `settings`): scan interval default, log cooldown, `light_profile_global`.

## Reload

Creating, updating, or deleting a camera via API calls `reload_cameras()`. The worker waits for in-flight OCR before restarting.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAMERA_RECONNECT_DELAY_SECONDS` | 5 | RTSP reconnect delay |
| `CAMERA_BUFFER_DRAIN` | 3 | Frames to drain per read (reduce latency) |
| `PLATE_DETECT_MAX_RETRIES` | 2 | Retries when OCR child returns no result |
| `PARKING_COOLDOWN_MAP_TTL_SECONDS` | 86400 | Prune in-memory cooldown keys |

## Health

- `GET /health` — database + worker summary
- `GET /api/live/status` — JWT; detailed worker status

## Troubleshooting

- **No logs:** check camera enabled, OCR not stuck (`ocr_busy` in live status), stability count in `.env`.
- **Slow scans:** CPU OCR is slow; use GPU (`PLATE_USE_GPU=auto`) and `docker-compose.gpu.yml`.
- **`.env` URL ignored:** database camera source wins after first bootstrap.
