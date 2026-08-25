"""One-off backfill of the identifier columns added by migration 001a.

This is the "migrate" step of the expand -> migrate -> contract migration and it
exists because of a chicken-and-egg problem: the loader now upserts on the new
identifier keys, but PostgreSQL cannot accept ON CONFLICT against columns that
are still null and not yet unique. So the rows have to be filled in place first.

It matches existing rows on the *old* natural key and copies the identifiers
from the freshly transformed staging files. Nothing is inserted or deleted, and
running it twice is harmless: the second run simply rewrites the same values.

Usage (inside the Airflow container, where PostgreSQL is reachable):
    python /opt/airflow/scripts/backfill_ids.py --input-dir /opt/airflow/data/staging
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_batch

from load import _python_value, get_connection


LOGGER = logging.getLogger("football_data_backfill")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# For each table: the UPDATE to run, and the staging columns feeding it in order.
# The WHERE clause uses the old natural key, which is still the primary key at
# this point, so every statement touches at most one row.
BACKFILL_STATEMENTS = {
    "matches": (
        """
        UPDATE matches
           SET home_team_id = %s, away_team_id = %s
         WHERE match_id = %s
        """,
        ["home_team_id", "away_team_id", "match_id"],
    ),
    "standings": (
        """
        UPDATE standings
           SET team_id = %s
         WHERE team_name = %s AND season = %s AND matchday = %s
        """,
        ["team_id", "team_name", "season", "matchday"],
    ),
    "scorers": (
        """
        UPDATE scorers
           SET player_id = %s, team_id = %s
         WHERE player_name = %s AND team = %s AND season = %s
        """,
        ["player_id", "team_id", "player_name", "team", "season"],
    ),
}


def _table_for(path: Path) -> str | None:
    """Map a staging filename to its destination table, or None if unrecognized."""
    return next((name for name in BACKFILL_STATEMENTS if f"_{name}_" in path.name), None)


def backfill_file(cursor, parquet_path: Path, table_name: str) -> int:
    """Apply the identifier backfill for one staging file and return rows touched."""
    statement, columns = BACKFILL_STATEMENTS[table_name]
    frame = pd.read_parquet(parquet_path, columns=columns)

    # Skip rows whose identifiers never made it through the transform; the
    # migration guard in 001b reports them rather than letting them slip past.
    frame = frame.dropna()
    if frame.empty:
        return 0

    # pandas hands back numpy scalars, and numpy.int64 does not inherit from
    # Python's int, so psycopg2 cannot adapt it. load._python_value already
    # unwraps them via .item(); reusing it keeps one conversion rule in the
    # project instead of two that can drift apart.
    rows = [
        tuple(_python_value(value) for value in record)
        for record in frame.itertuples(index=False, name=None)
    ]

    # execute_batch groups the statements into few round trips instead of one
    # per row, which matters once the tables hold thousands of matches.
    execute_batch(cursor, statement, rows, page_size=500)

    # Report the number of statements sent, not cursor.rowcount: after
    # execute_batch the latter reflects only the final statement of the batch,
    # which would understate the work to the point of being misleading.
    return len(rows)


def main() -> int:
    """Backfill identifiers across every staging file, oldest first."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=REPOSITORY_ROOT / "data" / "staging")
    args = parser.parse_args()

    files = sorted(args.input_dir.glob("*.parquet"))
    if not files:
        LOGGER.error("No Parquet files found in %s", args.input_dir)
        return 1

    connection = get_connection()
    try:
        # The whole backfill is one transaction: either every table gains its
        # identifiers or the database is left exactly as it was.
        with connection:
            with connection.cursor() as cursor:
                for path in files:
                    table_name = _table_for(path)
                    if table_name is None:
                        LOGGER.warning("Skipping unrecognized file: %s", path.name)
                        continue
                    touched = backfill_file(cursor, path, table_name)
                    LOGGER.info("%-10s <- %-46s %4d row(s)", table_name, path.name, touched)
    finally:
        connection.close()

    LOGGER.info("Backfill completed over %d staging file(s)", len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
