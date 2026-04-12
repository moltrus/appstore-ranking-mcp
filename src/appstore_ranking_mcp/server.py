import json
import os
from datetime import datetime
from collections import defaultdict
from typing import Any, Dict
from email.utils import parsedate_to_datetime
from mcp.server.fastmcp import FastMCP


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
STORAGE_DIR = os.path.join(PROJECT_ROOT, "data", "app_data_historical")

mcp = FastMCP(
    "app-ranking",
    instructions=(
        "This server tracks historical App Store rankings for mobile applications."
        "It provides tools to view top apps live and analyze rank changes (timelines) over time."
    ),
)


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
            print(f"DEBUG: Error processing {f}: {e}")
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
        "timeline": []
    })

    for filepath, dt in files_with_timestamps:
        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            results = data.get("feed", {}).get("results", [])
            timestamp = dt.isoformat()

            for position, app in enumerate(results, start=1):
                app_id = app.get("id")
                app_name = app.get("name")
                artist_name = app.get("artistName")

                if app_id not in app_timelines:
                    app_timelines[app_id]["appName"] = app_name
                    app_timelines[app_id]["appId"] = app_id
                    app_timelines[app_id]["artistName"] = artist_name

                app_timelines[app_id]["timeline"].append({
                    "time": timestamp,
                    "rank": position
                })

        except (json.JSONDecodeError, IOError):
            continue

    return dict(app_timelines)


@mcp.tool()
def get_app_timeline_by_id(app_id: str, app_type: str = "free") -> Dict[str, Any]:
    """
    Get the historical rank timeline for a specific app ID.

    Args:
        app_id: The unique Apple App Store ID.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    app_timelines = build_app_timeline(app_type)

    if app_id not in app_timelines:
        return {}

    timeline_data = app_timelines[app_id]
    raw_timeline = timeline_data.get("timeline", [])
    rank_timeline = []

    for i in range(len(raw_timeline)):
        current_entry = raw_timeline[i]
        prev_entry = raw_timeline[i - 1] if i > 0 else None

        rank_change = None
        if prev_entry:
            rank_change = prev_entry["rank"] - current_entry["rank"]

        # Only record entry if rank changed or it's the first known detection
        if rank_change != 0 or prev_entry is None:
            entry = {
                "time": current_entry["time"],
                "rank": current_entry["rank"]
            }

            # Use the last recorded entry in rank_timeline as the baseline for timeSinceLastChange
            if rank_timeline:
                try:
                    curr_dt = datetime.fromisoformat(current_entry["time"])
                    last_recorded_dt = datetime.fromisoformat(rank_timeline[-1]["time"])
                    entry["timeSinceLastChange"] = str(curr_dt - last_recorded_dt)
                except (ValueError, TypeError):
                    entry["timeSinceLastChange"] = None

                entry["delta"] = rank_change
                if rank_change > 0:
                    entry["direction"] = "up"
                elif rank_change < 0:
                    entry["direction"] = "down"

            rank_timeline.append(entry)

    return {
        "appId": app_id,
        "appName": timeline_data.get("appName"),
        "artistName": timeline_data.get("artistName"),
        "rankTimeline": rank_timeline,
        "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    }


@mcp.tool()
def get_app_timeline_by_name(app_name: str, app_type: str = "free") -> Dict[str, Any]:
    """
    Get an app's timeline by its name.

    Args:
        app_name: The display name of the app.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    app_timelines = build_app_timeline(app_type)

    for app_id, timeline_data in app_timelines.items():
        if timeline_data.get("appName") == app_name:
            return get_app_timeline_by_id(app_id, app_type)

    return {}


@mcp.tool()
def get_top_n_apps(n: int = 10, app_type: str = "free") -> Dict[str, Any]:
    """
    Get the top N apps live from the latest available snapshot.

    Args:
        n: The number of apps to retrieve. Defaults to 10.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    files_with_timestamps = get_all_historical_files(app_type)

    if not files_with_timestamps:
        return {"apps": [], "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}

    latest_file, latest_dt = files_with_timestamps[-1]

    try:
        with open(latest_file, "r") as f:
            data = json.load(f)

        results = data.get("feed", {}).get("results", [])[:n]

        apps = [
            {
                "rank": i + 1,
                "appId": app.get("id"),
                "appName": app.get("name"),
                "artistName": app.get("artistName")
            }
            for i, app in enumerate(results)
        ]

        return {
            "apps": apps,
            "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        }
    except (json.JSONDecodeError, IOError):
        return {"apps": [], "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

