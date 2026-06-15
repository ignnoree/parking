# RBAC — API access

Who can call what. All protected APIs need a JWT (`Authorization: Bearer …` or login cookies).

**Roles**

| Role | ID | Typical user |
|------|-----|--------------|
| System admin | `system_admin` | IT / owner — full control |
| Parking admin | `parking_admin` | Site manager — cameras, logs, vehicles |
| Worker | `worker` | Gate staff — enroll vehicles, view logs |

Default bootstrap: `admin` / `1234` (`system_admin`).

---

## API access matrix

Legend: **SA** = system_admin · **PA** = parking_admin · **W** = worker · **—** = no login · **All** = any logged-in role

### Auth

| Method | API | Access |
|--------|-----|--------|
| POST | `/auth/login` | — |
| POST | `/auth/refresh` | Valid refresh token |
| GET | `/auth/me` | All |

### Public (no JWT)

| Method | API | Access |
|--------|-----|--------|
| GET | `/health` | — |
| GET | `/favicon.ico` | — |

### HTML pages (browser UI; APIs inside still need JWT)

| Method | Page | Who uses it |
|--------|------|-------------|
| GET | `/` | Monitor — login required for data |
| GET | `/login` | Everyone |
| GET | `/submit` | All (enroll form) |
| GET | `/vehicles` | All (vehicle list) |
| GET | `/admin` | SA, PA (cameras, logs, settings) |

### Live camera

| Method | API | Access |
|--------|-----|--------|
| GET | `/api/live/status` | All |
| GET | `/api/live/stream` | All |

### Parking logs

| Method | API | Access |
|--------|-----|--------|
| GET | `/api/parking-logs` | All |
| GET | `/api/parking-logs?include_deleted=true` | **SA only** |
| DELETE | `/api/parking-logs/<log_id>` | SA, PA |
| GET | `/api/parking-snapshot` | All |

### Vehicles

| Method | API | Access |
|--------|-----|--------|
| GET | `/api/vehicles` | All |
| POST | `/api/enroll` | SA, PA, W |
| POST | `/api/remove-vehicle` | SA, PA, W |

### Cameras & settings

| Method | API | Access |
|--------|-----|--------|
| GET | `/api/cameras` | SA, PA |
| POST | `/api/cameras` | SA, PA |
| PATCH | `/api/cameras/<id>` | SA, PA |
| DELETE | `/api/cameras/<id>` | SA, PA |
| GET | `/api/settings` | SA, PA |
| PATCH | `/api/settings/<key>` | SA, PA |

### Software logs

| Method | API | Access |
|--------|-----|--------|
| GET | `/api/software-logs` | SA, PA |

### User accounts

| Method | API | Access |
|--------|-----|--------|
| GET | `/api/admins` | SA |
| POST | `/api/admins` | SA |
| PATCH | `/api/admins/<id>` | SA |
| DELETE | `/api/admins/<id>` | SA |

### System

| Method | API | Access |
|--------|-----|--------|
| GET | `/reset` | SA (wipes DB + uploads) |

---

## By role (quick view)

| What | SA | PA | W |
|------|:--:|:--:|:--:|
| View parking logs & live stream | ✓ | ✓ | ✓ |
| View snapshots | ✓ | ✓ | ✓ |
| Enroll / remove vehicles | ✓ | ✓ | ✓ |
| Soft-delete parking logs | ✓ | ✓ | ✗ |
| Logs with deleted vehicles | ✓ | ✗ | ✗ |
| Cameras & settings | ✓ | ✓ | ✗ |
| Software logs | ✓ | ✓ | ✗ |
| Manage user accounts | ✓ | ✗ | ✗ |
| System reset | ✓ | ✗ | ✗ |

---

## Errors

| Code | Meaning |
|------|---------|
| `401` | Not logged in or bad token |
| `403` | Logged in but wrong role |

---

## How it works in code

- `@jwt_required()` — any valid login
- `@require_admin_roles(...)` — only listed roles (`helpers/rbac.py`)

Full request/response details: [API_DOCUMENTATION.md](API_DOCUMENTATION.md)
