"""Consume live match events from Kafka and persist them in the warehouse.

The consumer is idempotent by design. Every message carries the event id
StatsBomb assigned, and the insert conflicts on that id, so replaying a match --
or restarting the consumer from offset zero -- rewrites nothing and duplicates
nothing. Reprocessing a topic is a normal recovery step, and the pipeline has to
survive it without corrupting the table.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from kafka import KafkaConsumer
from kafka.errors import KafkaError
from psycopg2 import sql as psycopg2_sql
from psycopg2.extras import Json


LOGGER = logging.getLogger("barca_event_consumer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOPIC = "barca-live-events"
GROUP_ID = "barca-live-events-consumer"
DEFAULT_BOOTSTRAP_SERVERS = "localhost:9092"
MAX_DB_RETRIES = 3

# How often to summarise progress. Without this the consumer looks hung while it
# is skipping a replay, because duplicates produce no per-event output.
PROGRESS_EVERY = 25

INSERT_EVENT = """
    INSERT INTO live_events (
        event_id, match_id, period, minute, second, event_type,
        player_id, player_name, team_id, team_name, detail, event_ts, produced_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id) DO NOTHING
"""


def configure_logging() -> None:
    """Configure logs for consumer execution."""
    # Windows consoles default to cp1252, which cannot render the match emojis.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def install_shutdown_handler() -> None:
    """Turn SIGTERM into KeyboardInterrupt so shutdown runs the same path.

    Ctrl+C already raises KeyboardInterrupt, but "docker stop" and process
    managers send SIGTERM, which would otherwise kill the consumer before it
    reports its session totals.
    """
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))


def get_connection():
    """Reuse the project's PostgreSQL connection configuration."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    from db import get_connection as connect

    return connect()


def ensure_table() -> None:
    """Create the warehouse schema and the live event table if they do not exist."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    from db import get_schema

    connection = get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                # The connection's search_path points at the warehouse schema, so
                # an unqualified CREATE TABLE fails outright when that schema does
                # not exist yet. This makes the consumer safe to run first on a
                # fresh clone, before the batch pipeline has created anything.
                cursor.execute(
                    psycopg2_sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(
                        schema=psycopg2_sql.Identifier(get_schema())
                    )
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS live_events (
                        event_id UUID PRIMARY KEY,
                        match_id BIGINT NOT NULL,
                        period SMALLINT,
                        minute INTEGER NOT NULL,
                        second SMALLINT NOT NULL,
                        event_type TEXT NOT NULL,
                        player_id INTEGER,
                        player_name TEXT,
                        team_id INTEGER,
                        team_name TEXT,

                        -- Event-specific fields such as expected goals or the
                        -- incoming player. JSONB keeps the table stable when a
                        -- new event type brings attributes the others lack.
                        detail JSONB NOT NULL DEFAULT '{}'::jsonb,

                        -- Event time: when it happened on the pitch.
                        event_ts TIMESTAMPTZ,
                        -- Processing time: when the producer published it and
                        -- when this consumer stored it. The gap between these
                        -- and event_ts is the pipeline's end-to-end latency.
                        produced_at TIMESTAMPTZ,
                        ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute("CREATE INDEX IF NOT EXISTS live_events_match_idx ON live_events (match_id, minute)")
    finally:
        connection.close()


def insert_event_with_retry(event: dict[str, Any]) -> bool | None:
    """Insert one event.

    Returns True when a row was written, None when the event was already
    present, and False when it could not be stored after every retry.
    """
    values = (
        event["event_id"], event["match_id"], event.get("period"), event["minute"], event["second"],
        event["event_type"], event.get("player_id"), event.get("player_name"),
        event.get("team_id"), event.get("team_name"), Json(event.get("detail") or {}),
        event.get("event_ts"), event.get("produced_at"),
    )

    for attempt in range(1, MAX_DB_RETRIES + 1):
        connection = None
        try:
            connection = get_connection()
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(INSERT_EVENT, values)
                    # ON CONFLICT DO NOTHING reports zero affected rows when the
                    # event was already stored, which is how a replay is detected.
                    return True if cursor.rowcount else None
        except Exception as error:
            LOGGER.error("PostgreSQL attempt %d/%d failed: %s", attempt, MAX_DB_RETRIES, error)
            if attempt < MAX_DB_RETRIES:
                time.sleep(2**attempt)
        finally:
            if connection is not None:
                connection.close()

    LOGGER.error("Event discarded after %d PostgreSQL attempts: %s", MAX_DB_RETRIES, event["event_id"])
    return False


def format_event(event: dict[str, Any], scores: dict[int, dict[str, int]]) -> str:
    """Render an event for readable operational logs, tracking the running score."""
    detail = event.get("detail") or {}
    team = event.get("team_name") or "-"
    who = event.get("player_name") or "-"

    if event["event_type"] in ("GOAL", "OWN_GOAL"):
        match_score = scores.setdefault(event["match_id"], {})
        match_score[team] = match_score.get(team, 0) + 1
        board = "  ".join(f"{name} {goals}" for name, goals in sorted(match_score.items()))
        return f"{event['minute']:>3}'  GOAL  {who} [{team}]   |  {board}"

    labels = {
        "SHOT": "shot", "YELLOW_CARD": "yellow card", "SECOND_YELLOW": "second yellow",
        "RED_CARD": "RED CARD", "SUBSTITUTION": "substitution",
        "PERIOD_START": "period start", "PERIOD_END": "period end",
    }
    extra = f" -> {detail['replacement_name']}" if detail.get("replacement_name") else ""
    return f"{event['minute']:>3}'  {labels.get(event['event_type'], event['event_type'])}  {who} [{team}]{extra}"


def run() -> None:
    """Consume events continuously until the process is stopped."""
    load_dotenv(REPOSITORY_ROOT / ".env")
    install_shutdown_handler()
    ensure_table()

    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", DEFAULT_BOOTSTRAP_SERVERS).split(",")
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=servers,
        group_id=GROUP_ID,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda value: json.loads(value.decode("utf-8")),
    )

    scores: dict[int, dict[str, int]] = {}
    stored = skipped = failed = 0
    LOGGER.info("Listening to Kafka topic %s", TOPIC)

    try:
        for message in consumer:
            event = message.value
            outcome = insert_event_with_retry(event)
            if outcome is True:
                stored += 1
                LOGGER.info(format_event(event, scores))
            elif outcome is None:
                skipped += 1
                # Announce the first duplicate so a replay is visible in the log
                # rather than looking like the consumer stopped doing work.
                if skipped == 1:
                    LOGGER.info("Event %s is already stored: replay detected, duplicates will be skipped", event["event_id"])
            else:
                failed += 1

            processed = stored + skipped + failed
            if processed % PROGRESS_EVERY == 0:
                LOGGER.info("Progress: %d stored, %d duplicates ignored, %d failed", stored, skipped, failed)
    except KeyboardInterrupt:
        LOGGER.info("Consumer stopped by user")
    except KafkaError as error:
        LOGGER.error("Kafka consuming failed: %s", error)
        raise
    finally:
        LOGGER.info("Session totals: %d stored, %d duplicates ignored, %d failed", stored, skipped, failed)
        consumer.close()


if __name__ == "__main__":
    configure_logging()
    run()
