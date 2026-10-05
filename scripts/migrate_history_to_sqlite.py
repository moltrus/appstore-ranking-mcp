"""
One-time backfill: reads every existing apps_{free,paid}_YYYYMMDD_HHMMSS.json
file in data/app_data_historical/ and inserts it into data/appstore_history.db.

Safe to re-run: insert_snapshot() is a no-op for a (app_type, captured_at)
pair that's already in the DB.

Usage (from the repo root):
    python scripts/migrate_history_to_sqlite.py
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "appstore_ranking_mcp"))
import db as history_db  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORAGE_DIR = os.path.join(PROJECT_ROOT, "data", "app_data_historical")


def parse_timestamp_from_filename(filename: str, app_type: str) -> datetime:
    # Expected format: apps_{app_type}_YYYYMMDD_HHMMSS.json (UTC, per existing convention)
    timestamp_part = filename.replace(f"apps_{app_type}_", "").replace(".json", "")
    return datetime.strptime(timestamp_part, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def migrate():
    history_db.init_db()

    if not os.path.exists(STORAGE_DIR):
        print(f"No historical data directory found at {STORAGE_DIR}")
        return

    conn = history_db.get_connection()
    inserted = 0
    skipped_existing = 0
    skipped_error = 0
    total = 0

    try:
        for app_type in ("free", "paid"):
            filenames = sorted(
                f for f in os.listdir(STORAGE_DIR)
                if f.startswith(f"apps_{app_type}_") and f.endswith(".json")
            )
            print(f"[{app_type}] Found {len(filenames)} files to process...")

            for i, filename in enumerate(filenames, start=1):
                total += 1
                filepath = os.path.join(STORAGE_DIR, filename)
                try:
                    captured_at = parse_timestamp_from_filename(filename, app_type)
                    with open(filepath, "r") as f:
                        data = json.load(f)
                    feed = data.get("feed", {})
                    results = feed.get("results", [])
                    feed_updated = feed.get("updated")

                    snapshot_id = history_db.insert_snapshot(
                        conn, app_type, captured_at.isoformat(), feed_updated, filename, results
                    )
                    if snapshot_id is None:
                        skipped_existing += 1
                    else:
                        inserted += 1
                except (ValueError, json.JSONDecodeError, IOError) as e:
                    skipped_error += 1
                    print(f"  Skipping {filename}: {e}")

                if i % 500 == 0:
                    print(f"  ...{i}/{len(filenames)} processed")
    finally:
        conn.close()

    print(
        f"\nDone. total={total} inserted={inserted} "
        f"already_present={skipped_existing} errors={skipped_error}"
    )
    print(f"SQLite DB at: {history_db.DB_PATH}")


if __name__ == "__main__":
    migrate()
