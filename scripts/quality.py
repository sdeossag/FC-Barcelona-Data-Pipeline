"""Post-load data-quality checks executed against the PostgreSQL warehouse.

These checks are deliberately different from the ones in ``load.validate_quality``.
That function inspects a pandas DataFrame *before* it reaches the database and
answers "is this batch safe to write?". The checks here run *after* the write and
answer "is the warehouse coherent now?" -- covering freshness, key integrity and
business invariants that a single batch cannot reveal on its own.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from psycopg2 import sql
from psycopg2.extras import execute_values


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
    # The source has been observed returning a timestamp in the status field.
    # status decides whether a match is treated as played, so a malformed value
    # silently moves a fixture between the training and prediction sets.
    WarehouseCheck(
        name="matches_status_recognised",
        sql=(
            "SELECT COUNT(*) FROM matches WHERE status NOT IN "
            "('SCHEDULED','TIMED','IN_PLAY','PAUSED','FINISHED','POSTPONED',"
            "'SUSPENDED','CANCELLED','AWARDED','UNKNOWN')"
        ),
        passes=lambda value: value == 0,
        expectation="every match status must be one the API documents",
    ),
    # Penalties are a subset of goals, so they can never exceed the total.
    WarehouseCheck(
        name="scorers_penalties_within_goals",
        sql="SELECT COUNT(*) FROM scorers WHERE penalties > goals",
        passes=lambda value: value == 0,
        expectation="a scorer cannot have more penalty goals than total goals",
    ),
    # Two independent sources describe the same match: football-data.org gives
    # the final score and ESPN gives the event log. If the goals in the events do
    # not add up to the final score, the stream dropped or mislabelled a goal.
    # This is the check that exposed ESPN's "Goal - Header" and "Goal -
    # Free-kick" labels, which a first mapping missed. Matches the stream has not
    # covered are not compared, so an empty live_events table passes.
    WarehouseCheck(
        name="live_events_goals_match_final_scores",
        sql=(
            "SELECT COUNT(*) FROM ("
            " SELECT match_id, MIN(event_ts)::date AS played_on,"
            "        MAX(detail->>'league') AS league,"
            "        COUNT(*) FILTER (WHERE event_type IN ('GOAL', 'OWN_GOAL')) AS goals"
            " FROM live_events GROUP BY match_id) e"
            " JOIN matches m"
            "   ON m.status = 'FINISHED'"
            "  AND (m.home_team_id = 81 OR m.away_team_id = 81)"
            "  AND m.date::date BETWEEN e.played_on - 1 AND e.played_on + 1"
            "  AND m.competition = CASE e.league WHEN 'esp.1' THEN 'PD' ELSE 'CL' END"
            " WHERE e.goals <> m.home_goals + m.away_goals"
        ),
        passes=lambda value: value == 0,
        expectation="goals in the event stream must equal the final score of the same match",
    ),
)


def _run_check(cursor, check: WarehouseCheck) -> tuple[bool, int]:
    """Execute one check and return whether it passed alongside its value."""
    cursor.execute(sql.SQL(check.sql))
    value = cursor.fetchone()[0]
    return check.passes(value), value


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check, in the shape the results table stores."""

    name: str
    passed: bool
    observed_value: int | None
    expectation: str


def evaluate_checks(connection, checks: tuple[WarehouseCheck, ...] = CHECKS) -> list[CheckResult]:
    """Run every check and return one result per check, passed or not.

    All checks run even after the first failure, so one execution reports the
    full picture instead of forcing a fix-and-rerun cycle for each problem.
    """
    results: list[CheckResult] = []
    with connection.cursor() as cursor:
        for check in checks:
            passed, value = _run_check(cursor, check)
            observed = None if value is None else int(value)
            results.append(CheckResult(check.name, bool(passed), observed, check.expectation))

            if passed:
                LOGGER.info("PASS %-38s value=%s", check.name, observed)
            else:
                LOGGER.error("FAIL %-38s value=%s | %s", check.name, observed, check.expectation)

    failures = sum(1 for result in results if not result.passed)
    LOGGER.info("Warehouse checks completed: %d passed, %d failed", len(results) - failures, failures)
    return results


def save_check_results(connection, results: list[CheckResult],
                       run_id: UUID | None = None) -> UUID:
    """Store one row per check and return the run identifier.

    The run id is generated here rather than by the database so a caller can
    retry a write and keep the same identity in its own logs. Append-only: a
    result is a statement about the moment the check ran.
    """
    run_id = run_id or uuid.uuid4()
    values = [(str(run_id), result.name, result.passed, result.observed_value, result.expectation)
              for result in results]
    statement = """
        INSERT INTO quality_check_results (
            run_id, check_name, passed, observed_value, expectation
        ) VALUES %s
        ON CONFLICT (run_id, check_name) DO NOTHING
    """
    with connection:
        with connection.cursor() as cursor:
            execute_values(cursor, statement, values, page_size=100)

    LOGGER.info("Stored %d check result(s) under run %s", len(values), run_id)
    return run_id


def run_warehouse_checks(connection, checks: tuple[WarehouseCheck, ...] = CHECKS,
                         persist: bool = True) -> list[str]:
    """Run every check, persist the results and return the names that failed."""
    results = evaluate_checks(connection, checks)
    if persist:
        save_check_results(connection, results)
    return [result.name for result in results if not result.passed]


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
