"""
Shared SQLite storage layer for App Store ranking history.

Replaces the old one-JSON-file-per-change storage model. A snapshot is one
poll of a feed (free/paid) where the ranking order changed; rankings are the
1..50 rows belonging to that snapshot.

UNIQUE(snapshot_id, rank) and UNIQUE(snapshot_id, app_id) make duplicate
ranks or duplicate apps within a single snapshot impossible at the DB level.
"""
import os
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

if os.name == "nt":
    PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
    DB_PATH = os.path.join(PROJECT_ROOT, "data", "appstore_history.db")
else:
    PROJECT_ROOT = "/root/p_analysis/app_store"
    # Mirrors apps_ranking_monitor.py's STORAGE_DIR convention on Linux
    # (no "data/" prefix there) so mutagen can sync this one file directly.
    DB_PATH = os.path.join(PROJECT_ROOT, "appstore_history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_type TEXT NOT NULL,
    captured_at TEXT NOT NULL,      -- UTC ISO timestamp, from filename/poll time
    feed_updated TEXT,              -- Apple's own feed.updated timestamp, if present
    source_file TEXT,               -- original filename, kept for traceability/audit
    UNIQUE(app_type, captured_at)
);

CREATE TABLE IF NOT EXISTS rankings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    app_id TEXT NOT NULL,
    app_name TEXT,
    artist_name TEXT,
    artwork_url TEXT,
    UNIQUE(snapshot_id, rank),
    UNIQUE(snapshot_id, app_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_type_time ON snapshots(app_type, captured_at);
CREATE INDEX IF NOT EXISTS idx_rankings_snapshot ON rankings(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_rankings_app ON rankings(app_id, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_source_file ON snapshots(source_file);
"""


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def parse_feed_updated(feed_updated_str):
    if not feed_updated_str:
        return None
    try:
        return parsedate_to_datetime(feed_updated_str).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def insert_snapshot(conn, app_type: str, captured_at_iso: str, feed_updated_str, source_file: str, results: list):
    """
    Insert one snapshot + its rankings. `results` is the ordered list of app
    dicts as returned by Apple's feed (position in the list = rank).

    Returns the snapshot id, or None if this (app_type, captured_at) snapshot
    already exists (idempotent re-run safe, e.g. during migration).
    """
    cur = conn.cursor()
    # source_file also catches the same poll stored under a different
    # captured_at precision (monitor rows used to carry microseconds).
    cur.execute(
        "SELECT id FROM snapshots WHERE app_type = ? AND (captured_at = ? OR source_file = ?)",
        (app_type, captured_at_iso, source_file),
    )
    existing = cur.fetchone()
    if existing:
        return None

    feed_updated_iso = parse_feed_updated(feed_updated_str)

    cur.execute(
        "INSERT INTO snapshots (app_type, captured_at, feed_updated, source_file) VALUES (?, ?, ?, ?)",
        (app_type, captured_at_iso, feed_updated_iso, source_file),
    )
    snapshot_id = cur.lastrowid

    rows = []
    seen_ids = set()
    for position, app in enumerate(results, start=1):
        app_id = app.get("id")
        if app_id in seen_ids:
            # Apple feed returned the same app twice in one snapshot; keep the
            # first (higher) position and drop the duplicate rather than
            # violating UNIQUE(snapshot_id, app_id).
            continue
        seen_ids.add(app_id)
        rows.append((snapshot_id, position, app_id, app.get("name"), app.get("artistName"), app.get("artworkUrl100")))

    cur.executemany(
        "INSERT INTO rankings (snapshot_id, rank, app_id, app_name, artist_name, artwork_url) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return snapshot_id


def get_all_snapshots(conn, app_type: str):
    """Returns [(snapshot_id, datetime), ...] sorted ascending by captured_at."""
    cur = conn.execute(
        "SELECT id, captured_at FROM snapshots WHERE app_type = ? ORDER BY captured_at ASC",
        (app_type,),
    )
    return [(row["id"], datetime.fromisoformat(row["captured_at"])) for row in cur.fetchall()]


def get_latest_snapshot(conn, app_type: str):
    cur = conn.execute(
        "SELECT id, captured_at FROM snapshots WHERE app_type = ? ORDER BY captured_at DESC LIMIT 1",
        (app_type,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return (row["id"], datetime.fromisoformat(row["captured_at"]))


def get_rankings(conn, snapshot_id: int):
    """Returns [{rank, id, name, artistName, artworkUrl}, ...] ordered by rank."""
    cur = conn.execute(
        "SELECT rank, app_id, app_name, artist_name, artwork_url FROM rankings WHERE snapshot_id = ? ORDER BY rank ASC",
        (snapshot_id,),
    )
    return [
        {
            "rank": row["rank"],
            "id": row["app_id"],
            "name": row["app_name"],
            "artistName": row["artist_name"],
            "artworkUrl100": row["artwork_url"],
        }
        for row in cur.fetchall()
    ]


def get_dropped_apps(conn, app_type: str) -> list:
    """
    Apps that appeared in any snapshot of `app_type` but are absent from the
    latest snapshot. "Latest" is whatever the last poll contained, so this
    holds for any chart size (top 50, top 100, ...), not just 50.

    Returns [{id, name, artistName, lastRank, lastSeen, firstSeen}, ...],
    most recently dropped first.
    """
    latest = get_latest_snapshot(conn, app_type)
    if not latest:
        return []

    cur = conn.execute(
        """
        SELECT r.app_id, r.app_name, r.artist_name, r.rank AS last_rank,
               g.last_seen, g.first_seen
        FROM (
            SELECT r2.app_id, MAX(s2.captured_at) AS last_seen, MIN(s2.captured_at) AS first_seen
            FROM rankings r2
            JOIN snapshots s2 ON s2.id = r2.snapshot_id
            WHERE s2.app_type = ?
              AND r2.app_id NOT IN (SELECT app_id FROM rankings WHERE snapshot_id = ?)
            GROUP BY r2.app_id
        ) g
        JOIN snapshots s ON s.app_type = ? AND s.captured_at = g.last_seen
        JOIN rankings r ON r.snapshot_id = s.id AND r.app_id = g.app_id
        ORDER BY g.last_seen DESC, r.rank ASC
        """,
        (app_type, latest[0], app_type),
    )
    return [
        {
            "id": row["app_id"],
            "name": row["app_name"],
            "artistName": row["artist_name"],
            "lastRank": row["last_rank"],
            "lastSeen": row["last_seen"],
            "firstSeen": row["first_seen"],
        }
        for row in cur.fetchall()
    ]


def build_app_timeline_window(conn, app_type: str, start_iso: str, end_iso: str) -> dict:
    """
    Same shape as build_app_timeline, but scoped to snapshots in
    [start_iso, end_iso] plus the single latest snapshot's app set (needed to
    know which apps are "current" for gainers/losers comparisons).
    """
    cur = conn.execute(
        """
        SELECT s.captured_at, r.rank, r.app_id, r.app_name, r.artist_name, r.artwork_url
        FROM rankings r
        JOIN snapshots s ON s.id = r.snapshot_id
        WHERE s.app_type = ? AND s.captured_at BETWEEN ? AND ?
        ORDER BY s.captured_at ASC
        """,
        (app_type, start_iso, end_iso),
    )

    app_timelines = {}
    for row in cur.fetchall():
        app_id = row["app_id"]
        entry = app_timelines.setdefault(app_id, {
            "appName": row["app_name"],
            "appId": app_id,
            "artistName": row["artist_name"],
            "artworkUrl": row["artwork_url"],
            "timeline": [],
        })
        # Rows are time-ascending, so the last row wins: apps get renamed and
        # re-iconed over time, and labels should match the current listing.
        entry["appName"] = row["app_name"]
        entry["artistName"] = row["artist_name"]
        entry["artworkUrl"] = row["artwork_url"]
        entry["timeline"].append({"time": row["captured_at"], "rank": row["rank"]})

    return app_timelines


def build_app_timeline(conn, app_type: str) -> dict:
    """
    Returns {app_id: {appName, appId, artistName, artworkUrl, timeline: [{time, rank}, ...]}}
    equivalent to the old per-file-scan implementation, but as a single indexed join query.
    """
    cur = conn.execute(
        """
        SELECT s.captured_at, r.rank, r.app_id, r.app_name, r.artist_name, r.artwork_url
        FROM rankings r
        JOIN snapshots s ON s.id = r.snapshot_id
        WHERE s.app_type = ?
        ORDER BY s.captured_at ASC
        """,
        (app_type,),
    )

    app_timelines = {}
    for row in cur.fetchall():
        app_id = row["app_id"]
        entry = app_timelines.setdefault(app_id, {
            "appName": row["app_name"],
            "appId": app_id,
            "artistName": row["artist_name"],
            "artworkUrl": row["artwork_url"],
            "timeline": [],
        })
        # Rows are time-ascending, so the last row wins: apps get renamed and
        # re-iconed over time, and labels should match the current listing.
        entry["appName"] = row["app_name"]
        entry["artistName"] = row["artist_name"]
        entry["artworkUrl"] = row["artwork_url"]
        entry["timeline"].append({"time": row["captured_at"], "rank": row["rank"]})

    return app_timelines
