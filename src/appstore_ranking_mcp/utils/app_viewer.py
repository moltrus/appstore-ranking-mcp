import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from collections import defaultdict
from email.utils import parsedate_to_datetime
import urllib.parse
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
STORAGE_DIR = os.path.join(PROJECT_ROOT, "data", "app_data_historical")
PORT = 8000

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
            free_current = query_components.get("free_current", [None])[0]
            free_previous = query_components.get("free_previous", [None])[0]
            paid_current = query_components.get("paid_current", [None])[0]
            paid_previous = query_components.get("paid_previous", [None])[0]

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            data = get_app_data(free_current, free_previous, paid_current, paid_previous)
            self.wfile.write(json.dumps(data).encode())
        elif self.path == "/api/files":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            files = {
                "free": sorted([f for f in os.listdir(STORAGE_DIR) if f.startswith("apps_free_")], reverse=True),
                "paid": sorted([f for f in os.listdir(STORAGE_DIR) if f.startswith("apps_paid_")], reverse=True)
            }
            self.wfile.write(json.dumps(files).encode())
        elif self.path.startswith("/api/timeline"):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            # Simple parsing for app_type param
            app_type = "free"
            if "type=paid" in self.path:
                app_type = "paid"

            timeline_data = build_app_timeline(app_type)
            self.wfile.write(json.dumps(timeline_data).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(format % args)

def get_app_data(free_current=None, free_previous=None, paid_current=None, paid_previous=None):
    data = {"free": {}, "paid": {}}

    for app_type in ["free", "paid"]:
        files = sorted(
            [f for f in os.listdir(STORAGE_DIR) if f.startswith(f"apps_{app_type}_")],
            reverse=True
        )
        if not files:
            continue

        current_filename = free_current if app_type == "free" else paid_current
        previous_filename = free_previous if app_type == "free" else paid_previous

        # default to latest file if none provided or it doesn't exist
        if not current_filename or current_filename not in files:
            current_filename = files[0]

        try:
            with open(os.path.join(STORAGE_DIR, current_filename)) as f:
                current = json.load(f)
            current_results = current.get("feed", {}).get("results", [])
        except:
            continue

        data[app_type]["current_file"] = current_filename

        # logic to determine previous file
        previous_results = []
        best_previous_file = None

        if previous_filename and previous_filename in files:
            best_previous_file = previous_filename
            try:
                with open(os.path.join(STORAGE_DIR, previous_filename)) as f:
                    prev_data = json.load(f)
                previous_results = prev_data.get("feed", {}).get("results", [])
            except:
                best_previous_file = None
                previous_results = []

        # if previous file was not specified or loading failed, use the immediate previous file
        if not best_previous_file:
            start_index = files.index(current_filename) + 1 if current_filename in files else 1
            if start_index < len(files):
                best_previous_file = files[start_index]
                try:
                    with open(os.path.join(STORAGE_DIR, best_previous_file)) as f:
                        prev_data = json.load(f)
                    previous_results = prev_data.get("feed", {}).get("results", [])
                except:
                    best_previous_file = None
                    previous_results = []

        data[app_type]["previous_file"] = best_previous_file

        if previous_results:
            data[app_type]["changes"] = compare_rankings(current_results, previous_results)
        else:
                data[app_type]["changes"] = [
                    {"position": i + 1, "app": app, "change": 0, "previous_position": None}
                    for i, app in enumerate(current_results)
                ]

    return data

def compare_rankings(current, previous):
    prev_map = {app["id"]: i for i, app in enumerate(previous)}
    changes = []

    for current_pos, app in enumerate(current):
        app_id = app["id"]
        prev_pos = prev_map.get(app_id)

        if prev_pos is not None:
            change = prev_pos - current_pos
        else:
            change = None

        changes.append({
            "position": current_pos + 1,
            "app": {
                "id": app["id"],
                "name": app.get("name", "Unknown"),
                "artistName": app.get("artistName", ""),
                "artworkUrl100": app.get("artworkUrl100", ""),
                "url": app.get("url", "")
            },
            "change": change,
            "previous_position": prev_pos + 1 if prev_pos is not None else None
        })

    return changes


def get_all_historical_files(app_type: str) -> list:
    if not os.path.exists(STORAGE_DIR):
        return []

    files = [
        os.path.join(STORAGE_DIR, f) for f in os.listdir(STORAGE_DIR)
        if f.startswith(f"apps_{app_type}_") and f.endswith(".json")
    ]

    files_with_timestamps = []
    for f in files:
        try:
            with open(f, "r") as json_file:
                data = json.load(json_file)
                updated_str = data.get("feed", {}).get("updated")
                if updated_str:
                    dt = parsedate_to_datetime(updated_str)
                    files_with_timestamps.append((f, dt))
        except Exception as e:
            continue

    files_with_timestamps.sort(key=lambda x: x[1])
    return files_with_timestamps

def build_app_timeline(app_type: str) -> dict:
    files_with_timestamps = get_all_historical_files(app_type)
    if not files_with_timestamps:
        return {}

    app_timelines = defaultdict(lambda: {
        "appName": None,
        "appId": None,
        "artistName": None,
        "artworkUrl": None,
        "timeline": []
    })

    for filepath, dt in files_with_timestamps:
        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            results = data.get("feed", {}).get("results", [])
            timestamp = dt.isoformat()

            for position, app in enumerate(results, start=1):
                app_id = app["id"]
                if app_id not in app_timelines:
                    app_timelines[app_id]["appName"] = app.get("name")
                    app_timelines[app_id]["appId"] = app_id
                    app_timelines[app_id]["artistName"] = app.get("artistName")
                    app_timelines[app_id]["artworkUrl"] = app.get("artworkUrl100")

                timeline = app_timelines[app_id]["timeline"]
                timeline.append({
                    "time": timestamp,
                    "rank": position
                })
        except:
            continue
    return dict(app_timelines)

if __name__ == "__main__":
    try:
        os.chdir(os.path.dirname(__file__))
        server = HTTPServer(("localhost", PORT), AppViewerHandler)
        logger.info(f"Starting server at http://localhost:{PORT}")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.server_close()

