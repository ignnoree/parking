# Parking ANPR — Admin Guide

Guide for **parking_admin** and **system_admin** users managing cameras, monitoring, and day-to-day operation.

For roles and API permissions, see [rbac.md](rbac.md).

---

## Access

| Page | URL | Who |
|------|-----|-----|
| Monitor | `/` | Anyone (API calls need login) |
| Login | `/login` | All staff |
| Register vehicle | `/submit` | Logged-in staff |
| **Admin panel** | `/admin` | `parking_admin`, `system_admin` only |

Default bootstrap account (change password in production): `admin` / `1234` (`system_admin`).

---

## Roles (summary)

| Task | system_admin | parking_admin | worker |
|------|:---:|:---:|:---:|
| Configure cameras | ✓ | ✓ | ✗ |
| Register / remove vehicles | ✓ | ✓ | ✓ |
| View logs & live stream | ✓ | ✓ | ✓ |
| System reset | ✓ | ✗ | ✗ |

---

## Admin panel (`/admin`)

Tabs: **Cameras**, **Parking logs**, **Software logs** (requires `parking_admin` or `system_admin`).

- **Parking logs** — filtered entry/exit history via `GET /api/parking-logs` (direction, match status, plate). `system_admin` can enable “Include soft-deleted vehicles”.
- **Software logs** — technical audit trail via `GET /api/software-logs` (level, event, module filters).

## Camera configuration (`/admin` → Cameras)

After the first server start, cameras are stored in the **database**. Use `/admin` for changes — not `.env` (env is only used to seed the first camera on install).

Saving a camera **reloads the camera worker** automatically.

### Fields

| Field | Description |
|-------|-------------|
| **Name** | Display label (e.g. “Entry gate”, “Exit B1”) |
| **Protocol** | `rtsp`, `http`, or `usb` |
| **Source** | URL or USB device index (see below) |
| **Gate role** | `entry` or `exit` — direction written to parking logs |
| **Light profile** | `normal`, `low_light`, or `high_glare` — see [Light profiles](#light-profiles) |
| **Frame interval** | Seconds between plate scans on this camera (empty = server default, usually 1.0 s; min 0.5) |
| **Enabled** | Off = camera ignored without deleting the row |

### Protocol + source examples

| Protocol | Source example | Use case |
|----------|----------------|----------|
| `usb` | `0`, `1` | Local USB webcam (device index) |
| `rtsp` | `rtsp://user:pass@192.168.1.50/stream1` | IP / NVR cameras |
| `http` | `http://192.168.1.50/snapshot.jpg` | HTTP snapshot or MJPEG URL |

### Multiple cameras

- You can enable **entry** and **exit** cameras separately.
- Each runs its own detection loop with its own gate role and light profile.
- The **live stream** on the monitor page uses the **first enabled camera** (lowest ID).

---

## Light profiles

Three options in the admin UI — there is no separate “mid” setting; **`normal` is the default (middle) profile**.

| UI value | When to use |
|----------|-------------|
| **normal** | Default — mixed daylight, covered parking |
| **low_light** | Night, dim garages, heavy shadows |
| **high_glare** | Direct sun, strong reflections on plate or windshield |

### What “brightness” means

The system measures **mean grayscale brightness** of each **plate crop** (the small image cut around the plate), not the whole scene.

- **Scale: 0–255** (0 = black, 255 = white)
- Evaluated **per frame**, not by clock time

### Profile behavior

#### `normal` (automatic)

Uses image brightness plus server threshold `PLATE_NIGHT_BRIGHTNESS_THRESHOLD` (default **85**, configurable in server `.env` only).

| Plate crop brightness | Night boost applied? | Contrast strength (CLAHE) |
|----------------------|----------------------|---------------------------|
| **≥ 85** | No | Standard (clip 2.5) |
| **55 – 84** | Yes | Stronger (clip 3.5) |
| **< 55** | Yes | Strongest gamma lift + clip 3.5 |

#### `low_light` (always dark treatment)

- **Always** applies night-style preprocessing
- Ignores automatic brightness detection
- Contrast: clip **3.0** / **4.0** (with night boost)

Use when the camera location is consistently dark.

#### `high_glare` (always bright / sunny)

- **Never** applies night boost (avoids making glare worse)
- Strongest contrast: clip **3.5** / **4.5**

Use when plates look washed out or reflective.

### Quick decision guide

```
Missed reads at night?           → low_light
Washed-out / shiny plates?       → high_glare
Unsure / mixed conditions?       → normal
```

---

## Monitor page (`/`)

- Shows a **table of parking events** (plate, direction, status, snapshot).
- **Show live camera** — optional MJPEG; detection on the server runs either way.
- Overlay box colors on live view:
  - **Green** — registered resident
  - **Yellow** — registered guest
  - **Red** — plate read but not in database (unregistered)

Requires login (JWT) for log refresh and live stream API calls.

---

## Registering vehicles (`/submit`)

| Type | Notes |
|------|-------|
| **Resident** | Stays until manually removed |
| **Guest** | Requires **expiry date**; ignored after expiry |

`worker`, `parking_admin`, and `system_admin` can all register vehicles.

---

## Parking log behavior

| Setting | Default | Where | Purpose |
|---------|---------|-------|---------|
| `PARKING_LOG_COOLDOWN_SECONDS` | 600 | DB (`/api/settings`) | Same plate not logged again for 10 minutes |
| `PLATE_OCR_MIN_CONFIDENCE` | 0.45 | Code (`plate_pipeline.py`) | Minimum combined confidence to accept a plate read |
| `CAMERA_FRAME_INTERVAL_SECONDS` | 1.0 | DB (`/api/settings`) | Default seconds between plate scans |
| `light_profile_global` | normal | DB (`/api/settings`) | Default glare/night preprocess profile |
| `PARKING_JITTER_COOLDOWN_SECONDS` | 20 | `.env` | Suppresses noisy duplicate reads of the same plate |
| `PARKING_READ_STABILITY_COUNT` | 2 | `.env` | Unregistered plates need repeated similar reads before logging |
| `PARKING_READ_STABILITY_WINDOW_SECONDS` | 8 | `.env` | Time window for stability check |

**Too many false unregistered logs?** Raise confidence or stability count.  
**Missed plates?** Try `low_light` or lower confidence (with care).

---

## OCR preprocessing (automatic)

Applied on the server before reading plate characters:

| Step | Purpose |
|------|---------|
| Perspective correction | Straightens tilted plates |
| Divider suppression | Removes lines OCR misreads as `=` or `\|` |
| Deblur / sharpen | Sharpens character edges |
| Contrast (CLAHE) | Strength depends on light profile |
| Night boost | Gamma lift for dark crops (when `night` mode applies) |
| Upscale | Enlarges crop (default 2.5×) for better OCR |

Toggles such as `PLATE_PREPROCESS_DEBLUR` live in server `.env`.

---

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| Camera won’t connect | Check protocol, URL/USB index, network, credentials |
| No parking logs | Camera enabled? Check worker status on `/admin` |
| Wrong entry vs exit | Fix **gate role** on that camera |
| Bad reads at night | Set light profile to **low_light** |
| Bad reads in sun | Set light profile to **high_glare** |
| Live view empty | At least one enabled camera; first camera must connect |
| Duplicate logs | Increase cooldown (server `.env`) |

---

## First install vs daily operation

1. **First start** — if the database has no cameras, one is created from `.env` (`CAMERA_URL`, `GATE_DIRECTION`, or `CAMERA_URL_ENTRY` / `CAMERA_URL_EXIT`).
2. **After that** — database is the source of truth; use **`/admin`** for camera changes.
