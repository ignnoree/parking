# Parking ANPR — API Reference

**Base URL:** `http://127.0.0.1:5000` (or your `PORT` / reverse proxy)

**Source files:** `routes/auth_routes.py`, `routes/app_routes.py`

JSON APIs return `Content-Type: application/json` unless noted. HTML pages return `text/html`.

---

## Authentication

Staff APIs use **JWT** via [Flask-JWT-Extended](https://flask-jwt-extended.readthedocs.io/).

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| POST | `/auth/login` | None | — |
| POST | `/auth/refresh` | Refresh token | — |
| GET | `/auth/me` | Access JWT | any |

### How to authenticate

Send the access token in either form:

- Header: `Authorization: Bearer <access_token>`
- Cookie: set automatically on login (`set_access_cookies`)

Refresh token: header `Authorization: Bearer <refresh_token>` with `@jwt_required(refresh=True)`, or refresh cookie.

**Token lifetimes (defaults in `main.py`):** access 15 min, refresh 7 days.

**Roles** (stored on admin account + embedded in JWT claims):

| Role | Value |
|------|-------|
| System admin | `system_admin` |
| Parking admin | `parking_admin` |
| Worker | `worker` |

Role checks use `@require_admin_roles(...)` from `helpers/rbac.py` — loads admin from DB by JWT identity and returns `401` / `403` if missing or not allowed.

---

### `POST /auth/login`

**Body:**

```json
{ "username": "admin", "password": "secret" }
```

**Success `200`:**

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "username": "admin",
  "role": "system_admin"
}
```

Also sets JWT cookies on the response.

**Errors:** `400` missing fields, `401` invalid credentials.

---

### `POST /auth/refresh`

**Auth:** Valid refresh token (rotates on each call; old refresh JTI invalidated in DB).

**Success `200`:**

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer"
}
```

**Errors:** `401` invalid or reused refresh token.

---

### `GET /auth/me`

**Success `200`:**

```json
{ "id": 1, "username": "admin", "role": "system_admin" }
```

**Errors:** `401` if JWT invalid or admin deleted.

---

## Common error shapes

| Status | Meaning |
|--------|---------|
| `400` | Bad request / validation |
| `401` | Missing or invalid JWT (`{"error": "Unauthorized"}` from RBAC) |
| `403` | Valid JWT but insufficient role (`{"error": "Forbidden"}`) |
| `404` | Resource not found |
| `409` | Conflict (duplicate username, camera source, etc.) |
| `503` | Service degraded (DB down, no camera for stream) |
| `500` | Unhandled server error (`{"error": "Server error"}`) |

Many app routes use `{"status": "error", "message": "..."}` instead of `{"error": ...}`.

---

## Health & status

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/health` | None | — |
| GET | `/api/live/status` | JWT | any |
| GET | `/favicon.ico` | None | — |

### `GET /health`

**Success `200`** (DB ok) / **`503`** (DB fail):

```json
{
  "status": "ok",
  "database": true,
  "camera": {
    "connected": true,
    "has_frame": true,
    "logged_flashes": 0,
    "frame_sequence": 42
  },
  "camera_worker": {
    "running": true,
    "camera_count": 1,
    "cameras": [{ "id": 1, "name": "Gate", "direction": "entry", "light_profile": "normal", "is_primary": true }],
    "primary_camera_id": 1,
    "ocr_busy": false,
    "ocr_worker": { "pid": 12345, "alive": true, "busy": false }
  }
}
```

### `GET /api/live/status`

Same `camera` + `camera_worker` fields as `/health`, wrapped as:

```json
{ "status": "ok", "connected": true, "has_frame": true, ... }
```

---

## Parking logs

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/parking-logs` | JWT | any |
| DELETE | `/api/parking-logs/<log_id>` | JWT | system_admin, parking_admin |
| GET | `/api/parking-snapshot` | JWT | any |

### `GET /api/parking-logs`

Paginated entry/exit events.

**Query parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `page` | `1` | Page number (≥ 1) |
| `page_size` | `10` | 1–50 |
| `direction` | — | `entry` or `exit` |
| `match_status` | — | e.g. `registered`, `unregistered`, `uncertain` |
| `plate` | — | Substring on normalized plate |
| `from_date` / `from` | — | Start date filter |
| `to_date` / `to` | — | End date filter |
| `include_deleted` | `false` | `true` — include logs for soft-deleted vehicles (**system_admin only**) |

**Success `200`:**

```json
{
  "total": 120,
  "page": 1,
  "page_size": 10,
  "has_next": true,
  "has_prev": false,
  "logs": [
    {
      "id": 1,
      "plate_normalized": "12B34567",
      "direction": "entry",
      "match_status": "registered",
      "snapshot_url": "/api/parking-snapshot?path=uploads%2F...",
      "source_snapshot_url": "/api/parking-snapshot?path=uploads%2F...",
      "plate_color": "white",
      ...
    }
  ]
}
```

By default, logs linked to soft-deleted vehicles are hidden from the list.

**Errors:** `403` if `include_deleted=true` without `system_admin`.

### `DELETE /api/parking-logs/<log_id>`

Soft-deletes one parking log (`deleted_at` set). **Roles:** `system_admin`, `parking_admin`.

**Success `200`:** `{"status": "ok"}`  
**Errors:** `404` not found.

### `GET /api/parking-snapshot`

Serves an image file. **Query:** `path` — relative path under `uploads/` or `collection/` only (path traversal blocked).

**Success:** image bytes (`send_file`)  
**Errors:** `404` invalid or missing path.

---

## Vehicles

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/vehicles` | JWT | any |
| POST | `/api/enroll` | JWT | system_admin, parking_admin, worker |
| POST | `/api/remove-vehicle` | JWT | system_admin, parking_admin, worker |

### `GET /api/vehicles`

**Query:**

| Param | Default | Description |
|-------|---------|-------------|
| `page` | `1` | Page number |
| `page_size` | `50` | 1–100 |
| `plate` | — | Plate filter |
| `owner` | — | Owner name filter |
| `is_guest` | — | `true` / `1` / `yes` / `on` |

**Success `200`:**

```json
{
  "total": 5,
  "page": 1,
  "page_size": 50,
  "has_next": false,
  "has_prev": false,
  "vehicles": [
    {
      "id": 1,
      "plate_normalized": "12B34567",
      "owner_name": "...",
      "is_guest": false,
      "reference_image_url": "/api/parking-snapshot?path=..."
    }
  ]
}
```

Only non-deleted vehicles are listed.

### `POST /api/enroll`

Register a resident or guest vehicle.

**Content-Type:** `application/json` **or** `multipart/form-data` (optional `reference_image` file).

**JSON body fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `plate_number` | yes | Raw plate text |
| `is_guest` | no | Default `false` |
| `guest_expires_at` | if guest | ISO datetime |
| `owner_name`, `owner_lastname` | no | |
| `car_model`, `door_number`, `floor_number`, `parking_spot` | no | |
| `plate_color` | no | Default `default` |
| `vehicle_class` | no | `car`, `motorcycle`, `other` (default `car`) |
| `metadata` | no | JSON object (or JSON string in form) |

**Success `200` (new):**

```json
{
  "status": "ok",
  "duplicate": false,
  "vehicle_id": 12,
  "plate_number_normalized": "12B34567",
  "reference_image_path": "collection/..."
}
```

**Success `200` (duplicate plate):**

```json
{
  "status": "ok",
  "duplicate": true,
  "vehicle_id": 5,
  "plate_number_normalized": "12B34567"
}
```

**Errors:** `400` validation.

### `POST /api/remove-vehicle`

Soft-delete by id or plate.

**Body:** `{ "vehicle_id": 12 }` or `{ "plate_number": "12ب34567" }`

**Success `200`:** `{"status": "ok"}`  
**Errors:** `404` not found.

---

## Cameras

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/cameras` | JWT | system_admin, parking_admin |
| POST | `/api/cameras` | JWT | system_admin, parking_admin |
| PATCH | `/api/cameras/<id>` | JWT | system_admin, parking_admin |
| DELETE | `/api/cameras/<id>` | JWT | system_admin, parking_admin |

Create/update/delete triggers `reload_cameras()` (worker restart after in-flight OCR).

### Camera fields

| Field | Values / notes |
|-------|----------------|
| `name` | Display name |
| `protocol` | `rtsp`, `http`, `usb` |
| `source` | URL, file path, or USB index |
| `direction` | `entry`, `exit` |
| `light_profile` | `normal`, `high_glare`, `low_light` |
| `is_enabled` | boolean |

Scan interval is **not** per-camera in the API — it comes from env `CAMERA_FRAME_INTERVAL_SECONDS` (see `.env.example`).

### `POST /api/cameras`

**Body example:**

```json
{
  "name": "Main gate",
  "protocol": "rtsp",
  "source": "rtsp://user:pass@192.168.1.10/stream",
  "direction": "entry",
  "light_profile": "normal",
  "is_enabled": true
}
```

**Success `201`:** `{"status": "ok", "camera": { ... }}`  
**Errors:** `400` invalid data, `409` duplicate source.

### `PATCH /api/cameras/<id>`

Partial update; any subset of allowed fields.

**Errors:** `400` empty body / invalid data, `404` not found, `409` duplicate source.

### `DELETE /api/cameras/<id>`

**Success `200`:** `{"status": "ok"}`  
**Errors:** `404` not found.

---

## Settings

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/settings` | JWT | system_admin, parking_admin |
| PATCH | `/api/settings/<key>` | JWT | system_admin, parking_admin |

Runtime settings stored in PostgreSQL `settings` table. Editable keys (`BOOTSTRAP_KEYS` in `database/settings_db.py`):

| Key | Default | Effect |
|-----|---------|--------|
| `PARKING_LOG_COOLDOWN_SECONDS` | `600` | Min seconds before same plate+direction logs again |
| `light_profile_global` | `normal` | Default preprocess profile |

Values are JSON objects, typically `{"value": "600"}`.

**Env-only (not in settings API):** `CAMERA_FRAME_INTERVAL_SECONDS`, OCR thresholds, tracker flags — see `.env.example`.

### `GET /api/settings`

```json
{
  "settings": [{ "key": "...", "value": {...}, "updated_at": "..." }],
  "allowed_keys": ["PARKING_LOG_COOLDOWN_SECONDS", "light_profile_global"],
  "options": {
    "protocols": ["rtsp", "http", "usb"],
    "directions": ["entry", "exit"],
    "light_profiles": ["normal", "high_glare", "low_light"]
  }
}
```

### `PATCH /api/settings/<key>`

**Body:**

```json
{ "value": { "value": "900" } }
```

A non-object `value` is wrapped as `{"value": <payload>}`.

**Success `200`:** `{"status": "ok", "setting": {"key": "...", "value": {...}}}`  
**Errors:** `400` unknown key or missing `value`.

---

## Software logs

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/software-logs` | JWT | system_admin, parking_admin |

Technical audit / ops log (auth failures, camera events, errors).

**Query:** `page` (default 1), `page_size` (1–100, default 50), `level`, `event`, `module`

**Success `200`:**

```json
{
  "total": 200,
  "page": 1,
  "page_size": 50,
  "has_next": true,
  "has_prev": false,
  "logs": [
    {
      "id": 1,
      "level": "INFO",
      "event": "auth.login.success",
      "module": "routes.auth_routes",
      "message": "User logged in",
      "logged_at": "..."
    }
  ]
}
```

---

## Admin accounts

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/admins` | JWT | system_admin |
| POST | `/api/admins` | JWT | system_admin |
| PATCH | `/api/admins/<id>` | JWT | system_admin |
| DELETE | `/api/admins/<id>` | JWT | system_admin |

Password hashes and `refresh_jti` are never returned.

### `POST /api/admins`

**Body:** `{ "username": "guard1", "password": "...", "role": "worker" }`

**Success `201`:** `{"status": "ok", "admin": { "id", "username", "role" }}`  
**Errors:** `400` validation, `409` username exists.

### `PATCH /api/admins/<id>`

**Body:** `{ "role": "parking_admin" }` and/or `{ "password": "new" }`

### `DELETE /api/admins/<id>`

Cannot delete self or the last `system_admin`.

**Errors:** `400` rule violation, `404` not found.

---

## Live video

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/api/live/stream` | JWT | any |

**Response:** `multipart/x-mixed-replace` MJPEG stream (`boundary=frame`).

**Errors:** `503` if no camera configured in DB.

Plate log overlays (colored boxes) are drawn on frames when a parking event is logged on the primary camera.

---

## System reset

| Method | Path | Auth | Roles |
|--------|------|------|-------|
| GET | `/reset` | JWT | system_admin |

**Destructive:** Wipes DB tables (`reset_db()`), clears `uploads/` and `collection/` files, reloads cameras.

**Success `200`:** `{"status": "ok"}`

---

## RBAC summary

| Capability | system_admin | parking_admin | worker |
|------------|:------------:|:-------------:|:------:|
| Parking logs (read) | yes | yes | yes |
| Parking log soft-delete | yes | yes | no |
| `include_deleted` on logs | yes | no | no |
| Vehicles (read) | yes | yes | yes |
| Enroll / remove vehicle | yes | yes | yes |
| Cameras & settings | yes | yes | no |
| Software logs | yes | yes | no |
| Admin CRUD | yes | no | no |
| System reset | yes | no | no |
| Live stream / status | yes | yes | yes |

Endpoints with only `@jwt_required()` (no `@require_admin_roles`) allow **any** authenticated role.

---

## HTML pages (no JSON)

| Path | Template | Description |
|------|----------|-------------|
| `/` | `templates/ui.html` | Monitor — parking logs, live camera |
| `/login` | `templates/login.html` | Staff login |
| `/submit` | `templates/submit_ui.html` | Register vehicle |
| `/vehicles` | `templates/vehicles.html` | Vehicle list UI |
| `/admin` | `templates/admin.html` | Cameras, logs, software logs, settings |

These pages call the JSON APIs above from the browser (JWT in storage/cookies).

---

## Quick reference — all endpoints

| Method | Path |
|--------|------|
| POST | `/auth/login` |
| POST | `/auth/refresh` |
| GET | `/auth/me` |
| GET | `/health` |
| GET | `/favicon.ico` |
| GET | `/` |
| GET | `/login` |
| GET | `/submit` |
| GET | `/vehicles` |
| GET | `/admin` |
| GET | `/api/live/status` |
| GET | `/api/live/stream` |
| GET | `/api/cameras` |
| POST | `/api/cameras` |
| PATCH | `/api/cameras/<id>` |
| DELETE | `/api/cameras/<id>` |
| GET | `/api/settings` |
| PATCH | `/api/settings/<key>` |
| GET | `/api/software-logs` |
| GET | `/api/admins` |
| POST | `/api/admins` |
| PATCH | `/api/admins/<id>` |
| DELETE | `/api/admins/<id>` |
| GET | `/api/parking-logs` |
| DELETE | `/api/parking-logs/<log_id>` |
| GET | `/api/parking-snapshot` |
| GET | `/api/vehicles` |
| POST | `/api/enroll` |
| POST | `/api/remove-vehicle` |
| GET | `/reset` |

---

## Related docs

- [Admin guide](admin_guide.md)
- [Camera worker](camera_worker.md)
- [Plate pipeline flow](plate_pipeline_flow.md)
