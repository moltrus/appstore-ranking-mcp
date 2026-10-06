import json
import os
import sys
import time
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import urllib.parse
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import db as history_db  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PORT = 8000

# Timeline responses are cached briefly since the chart UI polls every 10s
# and the underlying data only changes when the monitor writes a new snapshot.
_TIMELINE_CACHE_TTL_SECONDS = 8
_timeline_cache = {}
_timeline_cache_lock = threading.Lock()


def get_cached_timeline(app_type: str) -> dict:
    now = time.monotonic()
    with _timeline_cache_lock:
        cached = _timeline_cache.get(app_type)
        if cached and (now - cached[0]) < _TIMELINE_CACHE_TTL_SECONDS:
            return cached[1]

    conn = history_db.get_connection()
    try:
        timeline = history_db.build_app_timeline(conn, app_type)
    finally:
        conn.close()

    with _timeline_cache_lock:
        _timeline_cache[app_type] = (now, timeline)
    return timeline


class AppViewerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        html_dir = os.path.dirname(__file__)
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            with open(os.path.join(html_dir, "app_viewer.html"), "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/chart":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            with open(os.path.join(html_dir, "chart_viewer.html"), "rb") as f:
                self.wfile.write(f.read())
        elif self.path.startswith("/api/data"):
            query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def _int_or_none(key):
                val = query_components.get(key, [None])[0]
                try:
                    return int(val) if val else None
                except ValueError:
                    return None

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            data = get_app_data(
                free_current=_int_or_none("free_current"),
                free_previous=_int_or_none("free_previous"),
                paid_current=_int_or_none("paid_current"),
                paid_previous=_int_or_none("paid_previous"),
            )
            self.wfile.write(json.dumps(data).encode())
        elif self.path == "/api/files":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            files = get_snapshot_list()
            self.wfile.write(json.dumps(files).encode())
        elif self.path.startswith("/api/timeline"):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            # Simple parsing for app_type param
            app_type = "free"
            if "type=paid" in self.path:
                app_type = "paid"

            timeline_data = get_cached_timeline(app_type)
            self.wfile.write(json.dumps(timeline_data).encode())
        elif self.path.startswith("/api/dropped"):
            query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            app_type = "paid" if query_components.get("type", ["free"])[0] == "paid" else "free"

            conn = history_db.get_connection()
            try:
                dropped = history_db.get_dropped_apps(conn, app_type)
            finally:
                conn.close()

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"count": len(dropped), "droppedApps": dropped}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(format % args)


def get_snapshot_list():
    """Returns {"free": [{"id", "time"}, ...], "paid": [...]}, newest first."""
    conn = history_db.get_connection()
    try:
        return {
            app_type: [
                {"id": sid, "time": dt.isoformat()}
                for sid, dt in reversed(history_db.get_all_snapshots(conn, app_type))
            ]
            for app_type in ("free", "paid")
        }
    finally:
        conn.close()


def get_app_data(free_current=None, free_previous=None, paid_current=None, paid_previous=None):
    """
    Returns rank data + changes for each app type. Callers may pin a specific
    snapshot id for "current"/"previous"; otherwise defaults to the latest
    snapshot vs. the one immediately before it.
    """
    data = {"free": {}, "paid": {}}
    requested = {
        "free": (free_current, free_previous),
        "paid": (paid_current, paid_previous),
    }

    conn = history_db.get_connection()
    try:
        for app_type in ("free", "paid"):
            snapshots = history_db.get_all_snapshots(conn, app_type)
            if not snapshots:
                continue

            by_id = {sid: dt for sid, dt in snapshots}
            requested_current, requested_previous = requested[app_type]

            if requested_current in by_id:
                current_id, current_dt = requested_current, by_id[requested_current]
            else:
                current_id, current_dt = snapshots[-1]

            current_results = history_db.get_rankings(conn, current_id)
            data[app_type]["current_id"] = current_id
            data[app_type]["current_time"] = current_dt.isoformat()

            previous_id = previous_dt = None
            if requested_previous in by_id:
                previous_id, previous_dt = requested_previous, by_id[requested_previous]
            else:
                idx = [sid for sid, _ in snapshots].index(current_id)
                if idx > 0:
                    previous_id, previous_dt = snapshots[idx - 1]

            if previous_id is not None:
                previous_results = history_db.get_rankings(conn, previous_id)
                data[app_type]["previous_id"] = previous_id
                data[app_type]["previous_time"] = previous_dt.isoformat()
                data[app_type]["changes"] = compare_rankings(current_results, previous_results)
            else:
                data[app_type]["previous_id"] = None
                data[app_type]["previous_time"] = None
                data[app_type]["changes"] = [
                    {"position": r["rank"], "app": _to_app_dict(r), "change": 0, "previous_position": None}
                    for r in current_results
                ]
    finally:
        conn.close()

    return data


def _to_app_dict(r):
    return {
        "id": r["id"],
        "name": r.get("name") or "Unknown",
        "artistName": r.get("artistName") or "",
        "artworkUrl100": r.get("artworkUrl100") or "",
        # Not stored in the DB; the slug-less form resolves to the same listing.
        "url": f"https://apps.apple.com/us/app/id{r['id']}",
    }


def compare_rankings(current, previous):
    prev_map = {r["id"]: r["rank"] for r in previous}
    changes = []

    for r in current:
        app_id = r["id"]
        prev_rank = prev_map.get(app_id)
        change = (prev_rank - r["rank"]) if prev_rank is not None else None

        changes.append({
            "position": r["rank"],
            "app": _to_app_dict(r),
            "change": change,
            "previous_position": prev_rank
        })

    return changes


if __name__ == "__main__":
    history_db.init_db()
    try:
        os.chdir(os.path.dirname(__file__))
        server = ThreadingHTTPServer(("localhost", PORT), AppViewerHandler)
        logger.info(f"Starting server at http://localhost:{PORT}")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.server_close()
