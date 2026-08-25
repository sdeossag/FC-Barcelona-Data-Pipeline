"""PostgreSQL connection settings shared by every part of the project.

This lives apart from load.py so the streaming services can open a connection
without importing pandas and pyarrow, which they never use. It also keeps the
schema decision in exactly one place: change POSTGRES_SCHEMA and the batch
pipeline, the streaming consumer and the quality checks all follow.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The warehouse lives in its own schema rather than in public, so analytics
# tables are separated from anything else the database may hold.
DEFAULT_SCHEMA = "warehouse"

# PostgreSQL identifiers: a letter or underscore, then letters, digits or
# underscores. Anything else is rejected before it reaches the connection.
SCHEMA_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def get_schema() -> str:
    """Return the warehouse schema name, validated for safe interpolation.

    The name goes into a libpq connection option rather than a query parameter,
    because search_path cannot be parameterized. Restricting it to identifier
    characters keeps a malformed .env from smuggling extra connection options
    through that string.
    """
    schema = os.getenv("POSTGRES_SCHEMA", DEFAULT_SCHEMA)
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(f"Invalid POSTGRES_SCHEMA value: {schema!r}")
    return schema


def get_connection():
    """Open a PostgreSQL connection scoped to the warehouse schema.

    Setting search_path on the connection means every statement in the project
    resolves to the configured schema without repeating it in each query.

    public is deliberately excluded from the path: if a table is missing from
    the warehouse schema, the query should fail loudly rather than quietly read
    a leftover copy left behind in public.
    """
    load_dotenv(REPOSITORY_ROOT / ".env")
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "barca_warehouse"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        options=f"-c search_path={get_schema()}",
    )
