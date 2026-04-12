import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
import httpx
from httpx import ReadTimeout

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logger = logging.getLogger(__name__)

# Configuration
URLS = [
    "https://rss.marketingtools.apple.com/api/v2/us/apps/top-free/50/apps.json",
    "https://rss.marketingtools.apple.com/api/v2/us/apps/top-paid/50/apps.json"
]
CHECK_INTERVAL = 60

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
STORAGE_DIR = os.path.join(PROJECT_ROOT, "data", "app_data_historical")

def get_hash_path(url: str) -> str:
    """Generate a stable hash file path for a URL."""
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"last_hash_{url_hash}.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache"
}

async def get_json_hash(content: bytes) -> str:
    """Calculate SHA-256 hash of the content."""
    return hashlib.sha256(content).hexdigest()

async def fetch_and_monitor(url: str):
    """Monitor a single JSON endpoint."""
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR)

    hash_file = get_hash_path(url)
    last_hash = None
    if os.path.exists(hash_file):
        with open(hash_file, "r") as f:
            last_hash = f.read().strip()

    logger.info(f"Starting monitor for {url}")

    async with httpx.AsyncClient(headers=HEADERS) as client:
        while True:
            try:
                logger.info(f"Checking for updates... {url}")
                response = await client.get(url, timeout=30.0)
                response.raise_for_status()

                response_data = response.json()
                results = response_data.get("feed", {}).get("results", [])
                current_hash = await get_json_hash(json.dumps(results, sort_keys=True).encode())

                if current_hash != last_hash:
                    app_type = "paid" if "top-paid" in url else "free"
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = os.path.join(STORAGE_DIR, f"apps_{app_type}_{timestamp}.json")

                    with open(filename, "w") as f:
                        json.dump(response_data, f, indent=4)

                    with open(hash_file, "w") as f:
                        f.write(current_hash)

                    logger.info(f"Change detected. Saved to {filename}")
                    last_hash = current_hash
                else:
                    logger.info(f"No change detected. {url}")

            except ReadTimeout:
                logger.warning(f"Request timeout for {url}. Retrying on next check.")
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    logger.warning(f"Server error {e.response.status_code} for {url}. Retrying on next check.")
                else:
                    logger.exception(f"HTTP error {e.response.status_code} for {url}")
            except Exception as e:
                logger.exception(f"Error during check for {url}")

            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    async def main():
        await asyncio.gather(*(fetch_and_monitor(url) for url in URLS))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user.")
