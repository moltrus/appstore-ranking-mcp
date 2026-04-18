import json
import logging
import os
from datetime import datetime
from collections import defaultdict
from typing import Any, Dict
from email.utils import parsedate_to_datetime
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
from toon import encode as toon_encode

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
STORAGE_DIR = os.path.join(PROJECT_ROOT, "data", "app_data_historical")

RESPONSE_FORMAT = os.getenv("RESPONSE_FORMAT", "json").lower()
if RESPONSE_FORMAT not in ("json", "toon"):
    RESPONSE_FORMAT = "json"

# ---------------------------------------------------------------------------
# Response formatting utilities
# ---------------------------------------------------------------------------

def _format_response(data: Any) -> Any:
    """
    Format response data according to RESPONSE_FORMAT setting.

    If RESPONSE_FORMAT is "toon", converts the data to TOON format (string).
    If RESPONSE_FORMAT is "json", returns data as-is (will be JSON serialized by MCP).

    Args:
        data: The response data (dict, list, or any JSON-serializable value)

    Returns:
        Formatted response data (string for TOON, original for JSON)
    """
    if RESPONSE_FORMAT == "toon":
        try:
            toon_str = toon_encode(data, {
                "indent": 2,
                "delimiter": ","
            })
            return toon_str
        except Exception as e:
            logger.warning("TOON encoding failed, falling back to JSON: %s", e)
            return data
    else:
        return data


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

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
            logger.debug(f"DEBUG: Error processing {f}: {e}")
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
def get_app_timeline_by_id(app_id: str, app_type: str = "free") -> Any:
    """
    Get the historical rank timeline for a specific app ID.

    Args:
        app_id: The unique Apple App Store ID.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    app_timelines = build_app_timeline(app_type)

    if app_id not in app_timelines:
        return _format_response({})

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

    return _format_response({
        "appId": app_id,
        "appName": timeline_data.get("appName"),
        "artistName": timeline_data.get("artistName"),
        "rankTimeline": rank_timeline,
        "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    })


@mcp.tool()
def get_app_timeline_by_name(app_name: str, app_type: str = "free") -> Any:
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

    return _format_response({})


@mcp.tool()
def get_top_n_apps(n: int = 10, app_type: str = "free") -> Any:
    """
    Get the top N apps live from the latest available snapshot.

    Args:
        n: The number of apps to retrieve. Defaults to 10.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    files_with_timestamps = get_all_historical_files(app_type)

    if not files_with_timestamps:
        return _format_response({"apps": [], "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')})

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

        return _format_response({
            "apps": apps,
            "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        })
    except (json.JSONDecodeError, IOError):
        return _format_response({"apps": [], "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')})


@mcp.tool()
def get_top_gainers_losers(time_period: str, limit: int = 10, app_type: str = "free", mode: str = "both") -> Any:
    """
    Get the top gainers and losers in app rankings over a specified time period.

    Args:
        time_period: The time period to analyze (e.g., '5m', '30m', '1h', '2d', '1w'). Supported units: m (minutes), h (hours), d (days), w (weeks).
        limit: The number of top gainers and losers to return. Defaults to 10.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
        mode: Determine what to return: 'gainers', 'losers', or 'both'. Defaults to 'both'.
    """
    import re
    from datetime import timedelta

    match = re.match(r"^(\d+)([mhdw])$", time_period.lower())
    if not match:
        return _format_response({"error": "Invalid time period format. Use format like '5m', '1h', '2d', '1w'."})

    value, unit = match.groups()
    value = int(value)

    if unit == 'm':
        td = timedelta(minutes=value)
    elif unit == 'h':
        td = timedelta(hours=value)
    elif unit == 'd':
        td = timedelta(days=value)
    elif unit == 'w':
        td = timedelta(weeks=value)

    app_timelines = build_app_timeline(app_type)

    rank_changes = []

    for app_id, data in app_timelines.items():
        timeline = data.get("timeline", [])
        if not timeline:
            continue

        latest_entry = timeline[-1]
        try:
            latest_entry_dt = datetime.fromisoformat(latest_entry["time"])
        except ValueError:
            continue

        target_dt_for_app = latest_entry_dt - td

        # Find the most recent entry that is at or before the target time
        closest_entry = None
        for entry in reversed(timeline):
            try:
                entry_dt = datetime.fromisoformat(entry["time"])
                if entry_dt <= target_dt_for_app:
                    closest_entry = entry
                    break
            except ValueError:
                continue

        if closest_entry and closest_entry != latest_entry:
            old_rank = closest_entry["rank"]
            new_rank = latest_entry["rank"]
            change = old_rank - new_rank

            if change != 0:
                rank_changes.append({
                    "appId": app_id,
                    "appName": data.get("appName"),
                    "artistName": data.get("artistName"),
                    "oldRank": old_rank,
                    "newRank": new_rank,
                    "rankChange": change
                })

    gainers = sorted([r for r in rank_changes if r["rankChange"] > 0], key=lambda x: x["rankChange"], reverse=True)[:limit]
    losers = sorted([r for r in rank_changes if r["rankChange"] < 0], key=lambda x: x["rankChange"])[:limit]

    result = {
        "timePeriod": time_period,
        "lastUpdated": datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    }

    if mode.lower() in ("both", "gainers"):
        result["gainers"] = gainers
    if mode.lower() in ("both", "losers"):
        result["losers"] = losers

    return _format_response(result)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

