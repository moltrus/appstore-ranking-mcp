import importlib.metadata
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any
from mcp.server.mcpserver import MCPServer
from dotenv import load_dotenv
from toon import encode as toon_encode

from . import db as history_db

# Windows opens stdio with cp1252 by default, which raises OSError("Invalid
# argument") on flush for non-ASCII output. MCP stdio requires UTF-8.
if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


history_db.init_db()

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

mcp = MCPServer(
    "app-ranking",
    version=importlib.metadata.version("appstore-ranking-mcp"),
    instructions=(
        "This server tracks historical App Store rankings for mobile applications."
        "It provides tools to view top apps live and analyze rank changes (timelines) over time."
    ),
)


def get_all_historical_snapshots(app_type: str) -> list:
    """
    Retrieves and sorts all snapshot ids for a given app type from SQLite.
    Returns [(snapshot_id, datetime), ...] sorted ascending, mirroring the
    old files_with_timestamps shape but backed by an indexed query instead
    of a full directory scan + per-file JSON parse.
    """
    conn = history_db.get_connection()
    try:
        return history_db.get_all_snapshots(conn, app_type)
    finally:
        conn.close()


def build_app_timeline(app_type: str) -> dict:
    conn = history_db.get_connection()
    try:
        return history_db.build_app_timeline(conn, app_type)
    finally:
        conn.close()


@mcp.tool()
def get_app_timeline_by_id(app_id: str, app_type: str = "free") -> Any:
    """
    Get the historical rank timeline for a specific app ID.
    Note: All times are managed and displayed in UTC.
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
            try:
                curr_dt = datetime.fromisoformat(current_entry["time"])
                day_name = curr_dt.strftime("%A")[0:3]
            except (ValueError, TypeError):
                day_name = None

            entry = {
                "time": current_entry["time"],
                "day": day_name,
                "rank": current_entry["rank"]
            }

            # Use the last recorded entry in rank_timeline as the baseline for timeSinceLastChange
            if rank_timeline:
                try:
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
        "lastUpdated": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
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
    search_name = app_name.lower()

    for app_id, timeline_data in app_timelines.items():
        current_name = (timeline_data.get("appName") or "").lower()
        if search_name in current_name:
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
    conn = history_db.get_connection()
    try:
        latest = history_db.get_latest_snapshot(conn, app_type)
        if not latest:
            return _format_response({"apps": [], "lastUpdated": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})

        latest_snapshot_id, latest_dt = latest
        rankings = history_db.get_rankings(conn, latest_snapshot_id)[:n]

        apps = [
            {
                "rank": r["rank"],
                "appId": r["id"],
                "appName": r["name"],
                "artistName": r["artistName"]
            }
            for r in rankings
        ]

        return _format_response({
            "apps": apps,
            "lastUpdated": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        })
    finally:
        conn.close()


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

    all_snapshots = get_all_historical_snapshots(app_type)
    if not all_snapshots:
        return _format_response({"error": "No historical data available."})

    _latest_snapshot_id, latest_dt = all_snapshots[-1]
    window_start_dt = latest_dt - td

    conn = history_db.get_connection()
    try:
        app_timelines = history_db.build_app_timeline_window(
            conn, app_type, window_start_dt.isoformat(), latest_dt.isoformat()
        )
    finally:
        conn.close()

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

        # Only compare apps that exist in the latest snapshot (current rankings).
        if latest_entry_dt != latest_dt:
            continue

        # Find the first entry within the window [window_start_dt, latest_dt].
        window_entry = None
        for entry in timeline:
            try:
                entry_dt = datetime.fromisoformat(entry["time"])
                if entry_dt >= window_start_dt:
                    window_entry = entry
                    break
            except ValueError:
                continue

        if window_entry and window_entry != latest_entry:
            old_rank = window_entry["rank"]
            new_rank = latest_entry["rank"]
            change = old_rank - new_rank

            if change != 0:
                rank_changes.append({
                    "appId": app_id,
                    "appName": data.get("appName"),
                    "artistName": data.get("artistName"),
                    "oldRank": old_rank,
                    "newRank": new_rank,
                    "rankChange": change,
                    "fromTime": window_entry.get("time"),
                    "toTime": latest_entry.get("time"),
                    # "fromFile": window_entry.get("sourceFile"),
                    # "toFile": os.path.splitext(os.path.basename(latest_file))[0]
                })

    gainers = sorted([r for r in rank_changes if r["rankChange"] > 0], key=lambda x: x["rankChange"], reverse=True)[:limit]
    losers = sorted([r for r in rank_changes if r["rankChange"] < 0], key=lambda x: x["rankChange"])[:limit]

    result = {
        "timePeriod": time_period,
        "lastUpdated": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    }

    if mode.lower() in ("both", "gainers"):
        result["gainers"] = gainers
    if mode.lower() in ("both", "losers"):
        result["losers"] = losers

    return _format_response(result)


@mcp.tool()
def get_rankings_by_datetime(target_datetime: str, limit: int = 50, app_type: str = "free", detailed: bool = False) -> Any:
    """
    Get the app rankings closest to a specific date and time, or detailed day-level insights.
    Note: All times are managed and displayed in UTC.
    Args:
        target_datetime: The target date and time (ISO format, e.g., '2026-04-06T14:30:00').
        limit: The number of apps to retrieve or check against. Defaults to 50.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
        detailed: If True, returns a summary of changes across the entire day of the target_datetime (new entries, dropouts, rank changes).
    """
    try:
        # Handle basic ISO formats and 'Z' timezone indicator
        target_dt = datetime.fromisoformat(target_datetime.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return _format_response({"error": "Invalid datetime format. Please use ISO format (e.g., '2026-04-06T14:30:00')."})

    all_snapshots = get_all_historical_snapshots(app_type)
    if not all_snapshots:
        return _format_response({"apps": [], "error": "No historical data available."})

    conn = history_db.get_connection()
    try:
        if not detailed:
            closest = None
            min_diff = None

            for snapshot_id, dt in all_snapshots:
                # Compute absolute time difference
                diff = abs((dt.replace(tzinfo=None) - target_dt).total_seconds())
                if min_diff is None or diff < min_diff:
                    min_diff = diff
                    closest = (snapshot_id, dt)

            if not closest:
                return _format_response({"apps": []})

            closest_snapshot_id, closest_dt = closest
            rankings = history_db.get_rankings(conn, closest_snapshot_id)[:limit]

            apps = [
                {
                    "rank": r["rank"],
                    "appId": r["id"],
                    "appName": r["name"],
                    "artistName": r["artistName"]
                }
                for r in rankings
            ]

            return _format_response({
                "targetDatetime": target_datetime,
                "closestMatchDatetime": closest_dt.isoformat(),
                "apps": apps
            })

        else:
            # Detailed mode: Summarize the whole day
            target_date = target_dt.date()
            day_snapshots = [(sid, dt) for sid, dt in all_snapshots if dt.date() == target_date]

            if not day_snapshots:
                return _format_response({"error": f"No historical data available for the date {target_date}."})

            # Compare the first and last snapshot of the day
            first_id, first_dt = day_snapshots[0]
            last_id, last_dt = day_snapshots[-1]

            first_results = history_db.get_rankings(conn, first_id)[:limit]
            last_results = history_db.get_rankings(conn, last_id)[:limit]

            start_apps = {r["id"]: {"rank": r["rank"], "appName": r["name"], "artistName": r["artistName"]} for r in first_results}
            end_apps = {r["id"]: {"rank": r["rank"], "appName": r["name"], "artistName": r["artistName"]} for r in last_results}

            entered = []
            left = []
            changed = []

            for app_id, end_info in end_apps.items():
                if app_id not in start_apps:
                    entered.append(end_info)
                else:
                    start_info = start_apps[app_id]
                    if start_info["rank"] != end_info["rank"]:
                        changed.append({
                            "appId": app_id,
                            "appName": end_info["appName"],
                            "artistName": end_info["artistName"],
                            "startRank": start_info["rank"],
                            "endRank": end_info["rank"],
                            "change": start_info["rank"] - end_info["rank"]  # Positive means moved up
                        })

            for app_id, start_info in start_apps.items():
                if app_id not in end_apps:
                    left.append(start_info)

            changed.sort(key=lambda x: x["startRank"])

            return _format_response({
                "targetDate": str(target_date),
                "snapshotsAnalyzed": len(day_snapshots),
                "startTime": first_dt.isoformat(),
                "endTime": last_dt.isoformat(),
                "insights": {
                    "newEntries": entered,
                    "dropouts": left,
                    "rankChanges": changed
                }
            })
    finally:
        conn.close()


@mcp.tool()
def get_new_entries(target_date: str = None, lookback_days: int = 1, limit: int = 50, app_type: str = "free") -> Any:
    """
    Get the apps that have newly entered the top rankings on a specific date compared to a previous date.
    Note: All times are managed and displayed in UTC.
    Args:
        target_date: The target date (e.g., '2026-04-28' or ISO format). Defaults to the latest available data.
        lookback_days: The number of days to look back to determine if an app is 'new'. Defaults to 1.
        limit: The number of top apps to check (up to 50). Defaults to 50.
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    from datetime import timedelta

    all_snapshots = get_all_historical_snapshots(app_type)
    if not all_snapshots:
        return _format_response({"error": "No historical data available."})

    if lookback_days < 1:
        return _format_response({"error": "lookback_days must be >= 1"})

    if limit < 1:
        return _format_response({"error": "limit must be >= 1"})
    if limit > 50:
        limit = 50

    # Resolve target snapshot
    if not target_date:
        target_id, target_dt = all_snapshots[-1]
    else:
        try:
            if len(target_date) == 10:  # YYYY-MM-DD
                parsed_target_date = datetime.fromisoformat(target_date).date()
                day_snapshots = [(sid, dt) for sid, dt in all_snapshots if dt.date() == parsed_target_date]
                if not day_snapshots:
                    return _format_response({"error": f"No data found for date: {target_date}"})
                target_id, target_dt = day_snapshots[-1]  # Latest snapshot of that day
            else:
                # ISO datetime: choose the latest snapshot at-or-before the requested time
                parsed_target_dt = datetime.fromisoformat(target_date.replace("Z", "+00:00"))
                eligible = [(sid, dt) for sid, dt in all_snapshots if dt <= parsed_target_dt]
                if eligible:
                    target_id, target_dt = eligible[-1]
                else:
                    target_id, target_dt = all_snapshots[0]
        except ValueError:
            return _format_response({"error": "Invalid target_date format. Use 'YYYY-MM-DD' or ISO datetime."})

    # Resolve baseline snapshot: use calendar-day lookback (latest snapshot on the baseline date).
    baseline_date = (target_dt - timedelta(days=lookback_days)).date()
    baseline_snapshots = [(sid, dt) for sid, dt in all_snapshots if dt.date() == baseline_date]

    # If we have no data for the baseline date, walk backwards until we find a day with data.
    if not baseline_snapshots:
        candidates = [(sid, dt) for sid, dt in all_snapshots if dt.date() < target_dt.date()]
        candidates.sort(key=lambda x: x[1])
        while candidates and candidates[-1][1].date() > baseline_date:
            candidates.pop()
        if candidates:
            baseline_id, baseline_dt = candidates[-1]
        else:
            baseline_id, baseline_dt = all_snapshots[0]
    else:
        baseline_id, baseline_dt = baseline_snapshots[-1]

    conn = history_db.get_connection()
    try:
        target_data = history_db.get_rankings(conn, target_id)[:limit]
        baseline_data = history_db.get_rankings(conn, baseline_id)[:limit]
    finally:
        conn.close()

    baseline_ids = {r["id"] for r in baseline_data}

    new_entries = []
    for r in target_data:
        if r["id"] not in baseline_ids:
            new_entries.append({
                "appId": r["id"],
                "appName": r["name"],
                "artistName": r["artistName"],
                "startRank": None,
                "endRank": r["rank"],
                "rankChange": None
            })

    return _format_response({
        "targetTime": target_dt.isoformat(),
        "baselineTime": baseline_dt.isoformat(),
        "lookbackDays": lookback_days,
        "newEntries": new_entries
    })

@mcp.tool()
def get_dropped_apps(app_type: str = "free") -> Any:
    """
    List every app that has appeared in the rankings at some point but is not in the latest snapshot
    (i.e. it dropped out of the chart). Works for any chart size, since it compares against whatever
    the latest snapshot contains.
    Note: All times are managed and displayed in UTC.
    Args:
        app_type: Either 'free' or 'paid'. Defaults to 'free'.
    """
    conn = history_db.get_connection()
    try:
        latest = history_db.get_latest_snapshot(conn, app_type)
        if not latest:
            return _format_response({"error": "No historical data available."})
        dropped = history_db.get_dropped_apps(conn, app_type)
    finally:
        conn.close()

    return _format_response({
        "asOf": latest[1].isoformat(),
        "count": len(dropped),
        "droppedApps": [
            {
                "appId": d["id"],
                "appName": d["name"],
                "artistName": d["artistName"],
                "lastRank": d["lastRank"],
                "lastSeen": d["lastSeen"],
                "firstSeen": d["firstSeen"],
            }
            for d in dropped
        ],
    })


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

