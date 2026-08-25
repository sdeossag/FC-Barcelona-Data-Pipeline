"""Load transformed Parquet datasets into PostgreSQL."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import execute_values


LOGGER = logging.getLogger("football_data_loader")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_TABLES = {"matches", "standings", "scorers"}
ALLOWED_STRATEGIES = {"append", "replace", "upsert"}

TABLE_DEFINITIONS = {
    "matches": {
        "columns": ["match_id", "date", "home_team", "away_team", "home_goals", "away_goals", "status", "competition", "matchday", "season"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS matches (
                match_id BIGINT PRIMARY KEY,
                date TIMESTAMPTZ NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER NOT NULL DEFAULT 0,
                away_goals INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                competition TEXT,
                matchday INTEGER,
                season INTEGER
            )
        """,
        "conflict_key": "match_id",
    },
    "standings": {
        "columns": ["position", "team_name", "played", "won", "drawn", "lost", "goals_for", "goals_against", "goal_diff", "points", "season", "matchday"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS standings (
                position INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                played INTEGER NOT NULL DEFAULT 0,
                won INTEGER NOT NULL DEFAULT 0,
                drawn INTEGER NOT NULL DEFAULT 0,
                lost INTEGER NOT NULL DEFAULT 0,
                goals_for INTEGER NOT NULL DEFAULT 0,
                goals_against INTEGER NOT NULL DEFAULT 0,
                goal_diff INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL DEFAULT 0,
                season INTEGER,
                matchday INTEGER,
                PRIMARY KEY (team_name, season, matchday)
            )
        """,
        "conflict_key": "team_name, season, matchday",
    },
    "scorers": {
        "columns": ["player_name", "team", "goals", "assists", "penalties", "season"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS scorers (
                player_name TEXT NOT NULL,
                team TEXT NOT NULL,
                goals INTEGER NOT NULL DEFAULT 0,
                assists INTEGER NOT NULL DEFAULT 0,
                penalties INTEGER NOT NULL DEFAULT 0,
                season INTEGER,
                PRIMARY KEY (player_name, team, season)
            )
        """,
        "conflict_key": "player_name, team, season",
    },
}


def configure_logging() -> None:
    """Configure logs for command-line and Airflow execution."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def get_connection():
    """Open a PostgreSQL connection using the repository .env configuration."""
    load_dotenv(REPOSITORY_ROOT / ".env")
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "barca_warehouse"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def validate_quality(df: pd.DataFrame, table_name: str) -> bool:
    """Run basic checks and return False when the dataset must not be loaded."""
    if df.empty:
        LOGGER.error("Quality check failed for %s: DataFrame is empty", table_name)
        return False

    critical_columns = {
        "matches": ["match_id", "date", "home_team", "away_team"],
        "standings": ["position", "team_name"],
        "scorers": ["player_name", "team"],
    }.get(table_name, [])
    missing_columns = [column for column in critical_columns if column not in df.columns]
    if missing_columns:
        LOGGER.error("Quality check failed for %s: missing columns %s", table_name, missing_columns)
        return False
    if df[critical_columns].isnull().any().any():
        LOGGER.error("Quality check failed for %s: null values in critical columns", table_name)
        return False

    non_negative_columns = {
        "matches": ["home_goals", "away_goals"],
        "standings": ["points"],
        "scorers": ["goals", "assists", "penalties"],
    }.get(table_name, [])
    for column in non_negative_columns:
        if column in df.columns and (pd.to_numeric(df[column], errors="coerce") < 0).any():
            LOGGER.error("Quality check failed for %s: negative values in %s", table_name, column)
            return False
    return True


def _python_value(value: Any) -> Any:
    """Convert pandas missing values and scalar timestamps for psycopg2."""
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value.item() if hasattr(value, "item") else value


def _ensure_table(cursor, table_name: str) -> None:
    """Create a supported destination table if it does not exist."""
    cursor.execute(TABLE_DEFINITIONS[table_name]["ddl"])


def _insert_rows(cursor, df: pd.DataFrame, table_name: str, strategy: str) -> None:
    """Insert rows using one parameterized bulk statement."""
    definition = TABLE_DEFINITIONS[table_name]
    columns = definition["columns"]
    values = [tuple(_python_value(row[column]) for column in columns) for _, row in df.iterrows()]
    table = sql.Identifier(table_name)
    column_sql = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
    if strategy == "upsert":
        conflict_columns = [part.strip() for part in definition["conflict_key"].split(",")]
        update_columns = [column for column in columns if column not in conflict_columns]
        update_sql = sql.SQL(", ").join(
            sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(column))
            for column in update_columns
        )
        statement = sql.SQL("INSERT INTO {table} ({columns}) VALUES %s ON CONFLICT ({conflict}) DO UPDATE SET {updates}").format(
            table=table,
            columns=column_sql,
            conflict=sql.SQL(", ").join(sql.Identifier(column) for column in conflict_columns),
            updates=update_sql,
        )
    else:
        statement = sql.SQL("INSERT INTO {table} ({columns}) VALUES %s").format(table=table, columns=column_sql)
    execute_values(cursor, statement.as_string(cursor.connection), values)


def load_to_postgres(parquet_path: str | Path, table_name: str, strategy: str) -> bool:
    """Validate and load a Parquet file using append, replace, or upsert."""
    table_name = table_name.lower()
    strategy = strategy.lower()
    if table_name not in ALLOWED_TABLES or strategy not in ALLOWED_STRATEGIES:
        LOGGER.error("Unsupported table or strategy: table=%s strategy=%s", table_name, strategy)
        return False
    try:
        df = pd.read_parquet(parquet_path)
    except (OSError, ValueError) as error:
        LOGGER.error("Could not read Parquet file %s: %s", parquet_path, error)
        return False
    if not validate_quality(df, table_name):
        LOGGER.error("Skipping load for %s because quality checks failed", parquet_path)
        return False

    connection = None
    try:
        connection = get_connection()
        with connection:
            with connection.cursor() as cursor:
                _ensure_table(cursor, table_name)
                if strategy == "replace":
                    cursor.execute(sql.SQL("TRUNCATE TABLE {table}").format(table=sql.Identifier(table_name)))
                _insert_rows(cursor, df, table_name, strategy)
        LOGGER.info("Loaded %d rows from %s into %s using %s", len(df), parquet_path, table_name, strategy)
        return True
    except (psycopg2.Error, OSError, ValueError) as error:
        if connection is not None:
            connection.rollback()
        LOGGER.error("PostgreSQL load failed for %s: %s", parquet_path, error)
        return False
    finally:
        if connection is not None:
            connection.close()


def main() -> int:
    """Load all staging Parquet files using upsert by default."""
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=REPOSITORY_ROOT / "data" / "staging")
    parser.add_argument("--strategy", choices=sorted(ALLOWED_STRATEGIES), default="upsert")
    args = parser.parse_args()
    success = True
    for path in sorted(args.input_dir.glob("*.parquet")):
        table_name = next((table for table in ALLOWED_TABLES if f"_{table}_" in path.name), None)
        if table_name is None:
            LOGGER.warning("Skipping unrecognized Parquet file: %s", path.name)
            continue
        success = load_to_postgres(path, table_name, args.strategy) and success
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
