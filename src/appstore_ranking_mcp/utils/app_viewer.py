import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(CURRENT_DIR)))
STORAGE_DIR = os.path.join(PROJECT_ROOT, "data", "app_data_historical")
HTML_FILE = os.path.join(CURRENT_DIR, "app_viewer.html")

PORT = 8000

class AppViewerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            with open(HTML_FILE, "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/api/data":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            data = get_app_data()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(format % args)

def get_app_data():
    data = {"free": {}, "paid": {}}

    for app_type in ["free", "paid"]:
        files = sorted(
            [f for f in os.listdir(STORAGE_DIR) if f.startswith(f"apps_{app_type}_")],
            reverse=True
        )

        if len(files) >= 2:
            with open(os.path.join(STORAGE_DIR, files[0])) as f:
                current = json.load(f)
            with open(os.path.join(STORAGE_DIR, files[1])) as f:
                previous = json.load(f)

            current_results = current.get("feed", {}).get("results", [])
            previous_results = previous.get("feed", {}).get("results", [])

            data[app_type]["current_file"] = files[0]
            data[app_type]["previous_file"] = files[1]
            data[app_type]["changes"] = compare_rankings(current_results, previous_results)
        elif len(files) == 1:
            with open(os.path.join(STORAGE_DIR, files[0])) as f:
                current = json.load(f)
            current_results = current.get("feed", {}).get("results", [])
            data[app_type]["current_file"] = files[0]
            data[app_type]["previous_file"] = None
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

if __name__ == "__main__":
    os.chdir(os.path.dirname(__file__))
    server = HTTPServer(("localhost", PORT), AppViewerHandler)
    logger.info(f"Starting server at http://localhost:{PORT}")
    server.serve_forever()
