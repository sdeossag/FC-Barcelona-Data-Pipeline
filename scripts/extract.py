"""Extract football data from football-data.org and persist raw JSON responses."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.football-data.org/v4"
REQUEST_INTERVAL_SECONDS = 60 / 10
DEFAULT_TIMEOUT_SECONDS = 30
LOGGER = logging.getLogger("football_data_extractor")
_LAST_REQUEST_AT: float | None = None


def configure_logging() -> None:
    """Configure concise, timestamped logs for command-line and Airflow use."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def load_dotenv(dotenv_path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding existing environment values."""
    if not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _wait_for_rate_limit() -> None:
    """Keep requests at or below the free API limit of ten requests per minute."""
    global _LAST_REQUEST_AT
    now = time.monotonic()
    if _LAST_REQUEST_AT is not None:
        remaining = REQUEST_INTERVAL_SECONDS - (now - _LAST_REQUEST_AT)
        if remaining > 0:
            LOGGER.debug("Rate limit pause: %.2f seconds", remaining)
            time.sleep(remaining)
    _LAST_REQUEST_AT = time.monotonic()


def _request_json(endpoint: str, api_key: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Request one endpoint and return its decoded JSON payload."""
    query = f"?{urlencode(params)}" if params else ""
    url = f"{API_BASE_URL}/{endpoint.lstrip('/')}" + query
    request = Request(url, headers={"X-Auth-Token": api_key, "Accept": "application/json"})

    _wait_for_rate_limit()
    LOGGER.info("Requesting %s", endpoint)
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("API response is not a JSON object")
            return payload
    except HTTPError as error:
        if error.code == 429:
            retry_after = error.headers.get("Retry-After")
            LOGGER.error("HTTP 429: API rate limit exceeded%s", f"; retry after {retry_after}s" if retry_after else "")
        elif error.code == 403:
            LOGGER.error("HTTP 403: API key is missing, invalid, or not authorized")
        elif error.code == 500:
            LOGGER.error("HTTP 500: football-data.org reported an internal server error")
        else:
            LOGGER.error("HTTP %s while requesting %s", error.code, endpoint)
        raise
    except URLError as error:
        LOGGER.error("Network error while requesting %s: %s", endpoint, error.reason)
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        LOGGER.error("Invalid response from %s: %s", endpoint, error)
        raise


def save_raw_response(payload: dict[str, Any], resource_name: str, output_dir: Path) -> Path:
    """Write a response to data/raw using a UTC timestamp in the filename."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"{resource_name}_{timestamp}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOGGER.info("Saved raw response to %s", output_path)
    return output_path


def get_matches(competition: str, api_key: str, season: int | None = None) -> dict[str, Any]:
    """Fetch matches for a competition, optionally filtered by season."""
    params = {"season": season} if season is not None else None
    return _request_json(f"competitions/{competition}/matches", api_key, params)


def get_standings(competition: str, api_key: str, season: int | None = None) -> dict[str, Any]:
    """Fetch standings for a competition, optionally filtered by season."""
    params = {"season": season} if season is not None else None
    return _request_json(f"competitions/{competition}/standings", api_key, params)


def get_scorers(competition: str, api_key: str, season: int | None = None) -> dict[str, Any]:
    """Fetch top scorers for a competition, optionally filtered by season."""
    params = {"season": season} if season is not None else None
    return _request_json(f"competitions/{competition}/scorers", api_key, params)


def run_test(api_key: str, output_dir: Path, competition: str, season: int | None) -> None:
    """Run one request per supported resource for a manual smoke test."""
    resources = (
        ("matches", get_matches),
        ("standings", get_standings),
        ("scorers", get_scorers),
    )
    for resource_name, extractor in resources:
        payload = extractor(competition, api_key, season)
        save_raw_response(payload, f"{competition.lower()}_{resource_name}", output_dir)
    LOGGER.info("Manual extraction test completed successfully")


def parse_args() -> argparse.Namespace:
    """Parse command-line options for manual execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Fetch matches, standings, and scorers once")
    parser.add_argument("--competition", default="PD", help="Competition code (default: PD)")
    parser.add_argument("--season", type=int, help="Optional season start year, for example 2025")
    parser.add_argument("--output-dir", type=Path, help="Output directory (default: repository/data/raw)")
    return parser.parse_args()


def main() -> int:
    """Run the manual extraction entry point."""
    configure_logging()
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    load_dotenv(repository_root / ".env")
    api_key = os.getenv("API_KEY_FOOTBALL")
    if not api_key or api_key == "your_api_key_here":
        LOGGER.error("API_KEY_FOOTBALL is not configured. Add it to .env or export it in the environment.")
        return 2
    if not args.test:
        LOGGER.info("No action requested. Use --test to run a manual extraction.")
        return 0

    output_dir = args.output_dir or repository_root / "data" / "raw"
    try:
        run_test(api_key, output_dir, args.competition.upper(), args.season)
    except (HTTPError, URLError, OSError, ValueError):
        LOGGER.error("Manual extraction test failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
