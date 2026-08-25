"""Post-load data-quality checks executed against the PostgreSQL warehouse.

These checks are deliberately different from the ones in ``load.validate_quality``.
That function inspects a pandas DataFrame *before* it reaches the database and
answers "is this batch safe to write?". The checks here run *after* the write and
answer "is the warehouse coherent now?" -- covering freshness, key integrity and
business invariants that a single batch cannot reveal on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from psycopg2 import sql


LOGGER = logging.getLogger("football_data_quality")

# A daily DAG should never leave the warehouse untouched for more than a day.
# The margin absorbs a late run without turning the check into noise.
MAX_STALENESS_HOURS = 30


@dataclass(frozen=True)
class WarehouseCheck:
    """One assertion about the warehouse, expressed as a single-value query.

    Every query returns exactly one number so the pass/fail decision stays simple
    and every failure can report the value that caused it.
    """

    name: str
    sql: str
    passes: Callable[[int], bool]
    expectation: str


# Queries return either a row count that must be positive, or a count of
# offending rows that must be zero. Nothing here modifies data.
CHECKS: tuple[WarehouseCheck, ...] = (
    # --- Presence: the load actually produced rows -------------------------
    WarehouseCheck(
        name="matches_present",
        sql="SELECT COUNT(*) FROM matches",
        passes=lambda value: value > 0,
        expectation="matches must contain at least one row",
    ),
    WarehouseCheck(
        name="standings_present",
        sql="SELECT COUNT(*) FROM standings",
        passes=lambda value: value > 0,
        expectation="standings must contain at least one row",
    ),
    WarehouseCheck(
        name="scorers_present",
        sql="SELECT COUNT(*) FROM scorers",
        passes=lambda value: value > 0,
        expectation="scorers must contain at least one row",
    ),

    # --- Freshness: the newest write is recent enough ----------------------
    # EXTRACT(EPOCH FROM interval) yields seconds; dividing by 3600 gives hours.
    # COALESCE covers an empty table, where MAX() returns NULL.
    WarehouseCheck(
        name="matches_freshness",
        sql=(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MAX(loaded_at))) / 3600, 9999)::int "
            "FROM matches"
        ),
        passes=lambda value: value <= MAX_STALENESS_HOURS,
        expectation=f"matches must have been loaded within {MAX_STALENESS_HOURS} hours",
    ),

    # --- Key integrity: the primary keys are doing their job ---------------
    WarehouseCheck(
        name="matches_unique_ids",
        sql="SELECT COUNT(*) - COUNT(DISTINCT match_id) FROM matches",
        passes=lambda value: value == 0,
        expectation="match_id must be unique across the matches table",
    ),

    # --- Business invariants: rules that must hold for real football -------
    # A team's played games must equal its wins plus draws plus losses.
    # A mismatch means the transform mapped a column to the wrong field.
    WarehouseCheck(
        name="standings_games_add_up",
        sql="SELECT COUNT(*) FROM standings WHERE played <> won + drawn + lost",
        passes=lambda value: value == 0,
        expectation="played must equal won + drawn + lost for every standings row",
    ),
    WarehouseCheck(
        name="standings_goal_difference_consistent",
        sql="SELECT COUNT(*) FROM standings WHERE goal_diff <> goals_for - goals_against",
        passes=lambda value: value == 0,
        expectation="goal_diff must equal goals_for - goals_against",
    ),
    WarehouseCheck(
        name="matches_no_negative_goals",
        sql="SELECT COUNT(*) FROM matches WHERE home_goals < 0 OR away_goals < 0",
        passes=lambda value: value == 0,
        expectation="goal counts must never be negative",
    ),
    # A finished match cannot be scheduled in the future. This catches timezone
    # handling bugs, which are the most common defect in date-heavy pipelines.
    WarehouseCheck(
        name="matches_finished_not_in_future",
        sql="SELECT COUNT(*) FROM matches WHERE status = 'FINISHED' AND date > NOW()",
        passes=lambda value: value == 0,
        expectation="matches marked FINISHED must not have a future date",
    ),
    # Penalties are a subset of goals, so they can never exceed the total.
    WarehouseCheck(
        name="scorers_penalties_within_goals",
        sql="SELECT COUNT(*) FROM scorers WHERE penalties > goals",
        passes=lambda value: value == 0,
        expectation="a scorer cannot have more penalty goals than total goals",
    ),
)


def _run_check(cursor, check: WarehouseCheck) -> tuple[bool, int]:
    """Execute one check and return whether it passed alongside its value."""
    cursor.execute(sql.SQL(check.sql))
    value = cursor.fetchone()[0]
    return check.passes(value), value


def run_warehouse_checks(connection, checks: tuple[WarehouseCheck, ...] = CHECKS) -> list[str]:
    """Run every check and return the names of the ones that failed.

    All checks run even after the first failure, so one execution reports the
    full picture instead of forcing a fix-and-rerun cycle for each problem.
    """
    failures: list[str] = []
    with connection.cursor() as cursor:
        for check in checks:
            passed, value = _run_check(cursor, check)
            if passed:
                LOGGER.info("PASS %-38s value=%s", check.name, value)
            else:
                failures.append(check.name)
                LOGGER.error("FAIL %-38s value=%s | %s", check.name, value, check.expectation)

    LOGGER.info("Warehouse checks completed: %d passed, %d failed", len(checks) - len(failures), len(failures))
    return failures


def main() -> int:
    """Run the checks from the command line for manual verification."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    from db import get_connection

    connection = get_connection()
    try:
        failures = run_warehouse_checks(connection)
    finally:
        connection.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
