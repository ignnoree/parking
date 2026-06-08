# Parking ANPR

Plate recognition parking gate system — Flask + PostgreSQL + server-side camera worker.

## Reset for testing

```bash
python scripts/reset_for_testing.py -y
```

## Quick start (local)

1. Create PostgreSQL database `parking_db`.
2. Copy `.env.example` → `.env` and set `DATABASE_URL`.
3. `pip install -r requirements.txt`
4. `python main.py`
5. Open http://127.0.0.1:5000 — default admin: `admin` / `1234`

## Plate detection (production — UAE / Dubai)

- **Detector:** YOLO `yolo-v9-s-608-license-plate-end2end` (all plates in frame)
- **OCR:** `ensemble` = global plate model + EasyOCR (`en,ar`)
- **Validation:** alphanumeric UAE/GCC-style plates (`A12345`, `DXB1234`, Arabic+digits)
- **Multi-plate:** every box in a frame is OCR'd and logged separately

```env
PLATE_OCR_BACKEND=ensemble
PLATE_OCR_MODEL=global-plates-mobile-vit-v2-model
PLATE_OCR_LANGS=en,ar
PLATE_DETECTOR_CONF=0.25
PLATE_FORMAT_MIN_SCORE=0.55
PLATE_DEBUG=true
```

Use `PLATE_OCR_BACKEND=fast` for plate-OCR only (no EasyOCR, faster startup).

## Snapshots

- `uploads/unknown_parking_logs/sources/scan_*_source.jpg` — full frame
- `uploads/unknown_parking_logs/crops/crop_*_*.jpg` — vehicle context crop per plate

## Docker

```bash
cp .env.example .env
docker compose up --build
```

App: http://127.0.0.1:5001
