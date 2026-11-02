"""Backfill past seasons of matches and scorers for the prediction module.

This is a one-off backfill, not an Airflow DAG. The daily pipeline keeps the
current season up to date; the model needs the seasons before it, and those
never change once played.

It is deliberately thin. Rate limiting, error handling and the raw-JSON layout
already live in extract.py, and duplicating them here would give the project two
extraction paths that drift apart. Everything below is a loop over seasons
calling the functions the daily pipeline already uses.

A note on coverage. The free tier does not serve the full archive: seasons
before 2023 return HTTP 403 for both competitions, which was confirmed against
the live API rather than assumed. That is why the range starts at 2023 and not,
as would be preferable for a model, a decade earlier.

USAGE (run once):
    python scripts/extract_historical.py
    python scripts/extract_historical.py --dry-run     # show the plan only

Expected runtime: about one minute, dominated by the API's 10 requests/minute
limit. Expected output: 16 JSON files under data/raw/historical/.

Safe to re-run: files already on disk are skipped, so an interrupted run
resumes where it stopped instead of re-fetching what it already has.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from extract import get_matches, get_scorers, load_dotenv


LOGGER = logging.getLogger("football_data_historical")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_DIR = REPOSITORY_ROOT / "data" / "raw" / "historical"

# Verified against the live API on 2026-09-04: 2021 and 2022 return HTTP 403 for
# both competitions, 2023 onward returns data. Widening this range without
# re-checking would just produce a run of 403s.
FIRST_AVAILABLE_SEASON = 2023
CURRENT_SEASON = 2026

COMPETITIONS = ("PD", "CL")

# One entry per resource: the extractor to call and the filename stem it writes.
# The stem keeps the "<competition>_<resource>_" shape the rest of the pipeline
# relies on to route a file to its destination table.
RESOURCES: dict[str, Callable[[str, str, int], dict[str, Any]]] = {
    "matches": get_matches,
    "scorers": get_scorers,
}


def configure_logging() -> None:
    """Configure timestamped logs for the backfill."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def target_path(competition: str, resource: str, season: int) -> Path:
    """Return where one season's raw response belongs.

    The filename is deterministic, unlike the timestamped names the daily
    extraction produces. That is what makes the skip-if-present check work: a
    re-run can tell whether it already has a season, which a timestamp never
    allows.
    """
    return HISTORICAL_DIR / competition / f"{competition.lower()}_{resource}_{season}.json"


def fetch_season(competition: str, resource: str, season: int, api_key: str) -> dict[str, Any] | None:
    """Fetch one resource for one season, or None when it is unavailable.

    A 403 means the season sits outside the free tier and no amount of retrying
    will change that, so it is reported and skipped rather than raised: one
    unavailable season must not abort a backfill of the others.
    """
    extractor = RESOURCES[resource]
    try:
        return extractor(competition, api_key, season)
    except HTTPError as error:
        if error.code == 403:
            LOGGER.warning("%s %s %s: not available on this API tier, skipping", competition, resource, season)
        elif error.code == 404:
            LOGGER.warning("%s %s %s: season not found, skipping", competition, resource, season)
        else:
            LOGGER.error("%s %s %s: HTTP %s", competition, resource, season, error.code)
        return None
    except (URLError, OSError, ValueError) as error:
        LOGGER.error("%s %s %s: %s", competition, resource, season, error)
        return None


def save_season(payload: dict[str, Any], competition: str, resource: str, season: int) -> Path:
    """Write one season's raw response to disk."""
    path = target_path(competition, resource, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _record_count(payload: dict[str, Any], resource: str) -> int:
    """Count the records a response carries, for the progress log."""
    return len(payload.get(resource) or [])


def backfill(api_key: str, dry_run: bool = False) -> dict[str, dict[str, int]]:
    """Fetch every missing season and return per-competition totals."""
    totals: dict[str, dict[str, int]] = {
        competition: {"seasons": 0, "records": 0, "skipped": 0} for competition in COMPETITIONS
    }

    for competition in COMPETITIONS:
        for season in range(FIRST_AVAILABLE_SEASON, CURRENT_SEASON + 1):
            for resource in RESOURCES:
                path = target_path(competition, resource, season)

                if path.is_file():
                    LOGGER.info("%s %s %s: already on disk, skipping", competition, resource, season)
                    totals[competition]["skipped"] += 1
                    continue

                if dry_run:
                    LOGGER.info("%s %s %s: would fetch -> %s", competition, resource, season, path.name)
                    continue

                payload = fetch_season(competition, resource, season, api_key)
                if payload is None:
                    continue

                save_season(payload, competition, resource, season)
                count = _record_count(payload, resource)
                totals[competition]["records"] += count
                if resource == "matches":
                    totals[competition]["seasons"] += 1
                LOGGER.info("%s %s %s: saved %d record(s)", competition, resource, season, count)

    return totals


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the backfill."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="List what would be fetched without calling the API")
    return parser.parse_args()


def main() -> int:
    """Run the historical backfill."""
    configure_logging()
    args = parse_args()

    load_dotenv(REPOSITORY_ROOT / ".env")
    api_key = os.getenv("API_KEY_FOOTBALL")
    if not api_key or api_key == "your_api_key_here":
        LOGGER.error("API_KEY_FOOTBALL is not configured. Add it to .env before running the backfill.")
        return 2

    started = time.monotonic()
    LOGGER.info(
        "Backfilling seasons %d-%d for %s%s",
        FIRST_AVAILABLE_SEASON, CURRENT_SEASON, ", ".join(COMPETITIONS),
        " (dry run)" if args.dry_run else "",
    )

    totals = backfill(api_key, dry_run=args.dry_run)
    elapsed = time.monotonic() - started

    LOGGER.info("Historical extraction complete:")
    for competition, counts in totals.items():
        LOGGER.info(
            "  %s: %d season(s) fetched, %d record(s), %d file(s) already present",
            competition, counts["seasons"], counts["records"], counts["skipped"],
        )
    LOGGER.info("  Elapsed: %.0f seconds", elapsed)
    LOGGER.info("  Files under: %s", HISTORICAL_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
