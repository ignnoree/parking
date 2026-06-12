"""Reset database and data folders for local testing.

Usage (from project root):
    python scripts/reset_for_testing.py
    python scripts/reset_for_testing.py -y

Stops are not required but avoid running while you need existing log data.
Recreates default admin: admin / 1234
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

from database.db import reset_db
from helpers.utils import COLLECTION_FOLDER, UPLOAD_FOLDER, ensure_upload_dirs


def _clear_folder(path: str) -> int:
    removed = 0
    if not os.path.isdir(path):
        return 0
    for name in os.listdir(path):
        item = os.path.join(path, name)
        if os.path.isfile(item) or os.path.islink(item):
            os.remove(item)
            removed += 1
        elif os.path.isdir(item):
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    return removed


def reset_testing_data(*, skip_confirm: bool = False) -> int:
    print("Parking test reset")
    print(f"  DATABASE_URL={os.environ.get('DATABASE_URL', '(not set)')}")
    print(f"  UPLOAD_FOLDER={UPLOAD_FOLDER}")
    print(f"  COLLECTION_FOLDER={COLLECTION_FOLDER}")

    if not skip_confirm:
        answer = input("Delete all DB rows and clear uploads/collection? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted.")
            return 1

    print("Resetting database (drop + recreate tables)...")
    reset_db()
    print("Database reset complete. Default admin: admin / 1234")

    for folder in (UPLOAD_FOLDER, COLLECTION_FOLDER):
        count = _clear_folder(folder)
        print(f"Cleared {os.path.abspath(folder)} ({count} item(s))")

    ensure_upload_dirs()
    print("Recreated uploads folder structure (temp, known/unknown parking logs).")
    print("Done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset DB and data folders for testing")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()
    return reset_testing_data(skip_confirm=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
