# Parking ANPR — API Documentation

Base URL: `http://127.0.0.1:5000` (or your `PORT` / reverse proxy).

All JSON APIs return `Content-Type: application/json` unless noted.

## Authentication

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/auth/login` | None | Login with `{ "username", "password" }`. Returns access + refresh tokens (also set as cookies). |
| POST | `/auth/refresh` | Refresh token | Rotate tokens. Old refresh token → `401`. |
| GET | `/auth/me` | JWT | Current user `{ id, username, role }`. |

**Headers:** `Authorization: Bearer <access_token>` (or JWT cookies from login).

**Roles:** `system_admin`, `parking_admin`, `worker` (see RBAC below).

**Token lifetimes (defaults):** access 15 minutes, refresh 7 days.

---

## Health & status

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | DB + camera worker status. |
| GET | `/api/live/status` | JWT | Stream + worker details. |

---

## Parking logs

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/parking-logs` | JWT | Paginated parking events. |
| GET | `/api/parking-snapshot` | JWT | Snapshot image (`?path=uploads/...`). |

### `GET /api/parking-logs`

Query parameters:

| Param | Description |
|-------|-------------|
| `page` | Page number (default `1`). |
| `page_size` | 1–50 (default `10`). |
| `direction` | `entry` or `exit`. |
| `match_status` | `registered` or `unregistered`. |
| `plate` | Substring filter on normalized plate. |
| `include_deleted` | `true` — include logs for **soft-deleted vehicles** (`system_admin` only). |

**Soft-delete behavior (default):** Logs are hidden when:

- The linked `vehicle_id` points to a vehicle with `deleted_at` set, or
- `plate_normalized` matches a soft-deleted vehicle’s plate.

This keeps removed vehicles out of the guard monitor and reports while preserving rows in the database.

---

## Vehicles

| Method | Path | Auth | Roles | Description |
|--------|------|------|-------|-------------|
| GET | `/api/vehicles` | JWT | all | List active (non-deleted) vehicles. |
| POST | `/api/enroll` | JWT | admin, worker | Register resident or guest vehicle. |
| POST | `/api/remove-vehicle` | JWT | admin, worker | Soft-delete by `vehicle_id` or `plate_number`. |

---

## Cameras

| Method | Path | Auth | Roles | Description |
|--------|------|------|-------|-------------|
| GET | `/api/cameras` | JWT | system_admin, parking_admin | List all cameras. |
| POST | `/api/cameras` | JWT | system_admin, parking_admin | Create camera. |
| PATCH | `/api/cameras/<id>` | JWT | system_admin, parking_admin | Update camera. |
| DELETE | `/api/cameras/<id>` | JWT | system_admin, parking_admin | Delete camera. |

Body fields: `name`, `protocol` (`rtsp`|`http`|`usb`), `source`, `gate_role` (`entry`|`exit`), `light_profile` (`normal`|`high_glare`|`low_light`), `frame_interval_seconds`, `is_enabled`.

---

## Settings (runtime configuration)

| Method | Path | Auth | Roles | Description |
|--------|------|------|-------|-------------|
| GET | `/api/settings` | JWT | system_admin, parking_admin | List settings + allowed keys. |
| PATCH | `/api/settings/<key>` | JWT | system_admin, parking_admin | Update one setting. |

Allowed keys:

| Key | Effect |
|-----|--------|
| `CAMERA_FRAME_INTERVAL_SECONDS` | Default scan interval when camera has no override (reloads worker). |
| `PARKING_LOG_COOLDOWN_SECONDS` | Seconds before same plate+direction logs again. |
| `light_profile_global` | Default glare/night preprocess profile. |

### What “settings DB drives runtime” means

Runtime tuning lives in PostgreSQL `settings` only — **not** in `.env`.

On first start, `bootstrap_default_settings()` inserts code defaults for any missing keys. After that, workers read the DB (via `helpers/runtime_settings.py`, ~2s cache). `PATCH /api/settings/...` clears the cache immediately.

**Hardcoded in code:** `PLATE_OCR_MIN_CONFIDENCE` (0.45) in `helpers/plate_pipeline.py` — combined detect×OCR gate.

**Still env-only today:** jitter cooldown, stability count, YOLO model name, Paddle backend, GPU flag, upload paths.

Example PATCH:

```http
PATCH /api/settings/CAMERA_FRAME_INTERVAL_SECONDS
Content-Type: application/json

{ "value": { "value": "1.5" } }
```

---

## Software logs (audit / ops)

| Method | Path | Auth | Roles | Description |
|--------|------|------|-------|-------------|
| GET | `/api/software-logs` | JWT | system_admin, parking_admin | Paginated technical logs. |

Query: `page`, `page_size` (max 100), `level`, `event`, `module`.

---

## Admin accounts

| Method | Path | Auth | Roles | Description |
|--------|------|------|-------|-------------|
| GET | `/api/admins` | JWT | system_admin | List staff (no password hashes). |
| POST | `/api/admins` | JWT | system_admin | Create `{ username, password, role }`. |
| PATCH | `/api/admins/<id>` | JWT | system_admin | Update `role` and/or `password`. |
| DELETE | `/api/admins/<id>` | JWT | system_admin | Delete account (not self; not last `system_admin`). |

---

## Live video

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/live/stream` | JWT | MJPEG multipart stream (optional UI). |

---

## System reset

| Method | Path | Auth | Roles | Description |
|--------|------|------|-------|-------------|
| GET | `/reset` | JWT | system_admin | Wipe DB tables + uploads (destructive). |

---

## RBAC summary

| Capability | system_admin | parking_admin | worker |
|------------|:------------:|:-------------:|:------:|
| Monitor / parking logs | yes | yes | yes |
| Enroll / remove vehicle | yes | yes | yes |
| Cameras & settings | yes | yes | no |
| Software logs | yes | yes | no |
| Admin user CRUD | yes | no | no |
| System reset | yes | no | no |
| `include_deleted` on parking logs | yes | no | no |

---

## Web pages (HTML)

| Path | Description |
|------|-------------|
| `/` | Monitor — recent parking logs, optional live camera. |
| `/login` | Staff login. |
| `/submit` | Register vehicle. |
| `/admin` | Cameras, parking log history, software logs. |

---

## Related docs

- [Admin guide](admin_guide.md)
- [Production requirements](../documents/production_requirements_fa_parking.md)
