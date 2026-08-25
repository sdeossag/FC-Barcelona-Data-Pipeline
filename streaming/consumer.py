"""Consume live football events from Kafka and persist them in PostgreSQL."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from kafka import KafkaConsumer
from kafka.errors import KafkaError
from psycopg2 import sql as psycopg2_sql


LOGGER = logging.getLogger("barca_event_consumer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOPIC = "barca-live-events"
GROUP_ID = "barca-live-events-consumer"
DEFAULT_BOOTSTRAP_SERVERS = "localhost:9092"
MAX_DB_RETRIES = 3


def configure_logging() -> None:
    """Configure logs for consumer execution."""
    # Windows consoles may default to cp1252, which cannot render football emojis.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def get_connection():
    """Reuse the project's PostgreSQL connection configuration."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    from load import get_connection as connect

    return connect()


def ensure_table() -> None:
    """Create the warehouse schema and the live event table if they do not exist."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    from load import get_schema

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
                        event_id BIGSERIAL PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        minute INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 120),
                        player TEXT NOT NULL,
                        team TEXT NOT NULL,
                        match_id BIGINT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
    finally:
        connection.close()


def insert_event_with_retry(event: dict[str, Any]) -> bool:
    """Insert one event, reconnecting up to three times after database errors."""
    for attempt in range(1, MAX_DB_RETRIES + 1):
        connection = None
        try:
            connection = get_connection()
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO live_events (event_type, minute, player, team, match_id)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (event["event_type"], event["minute"], event["player"], event["team"], event["match_id"]),
                    )
            return True
        except Exception as error:
            LOGGER.error("PostgreSQL attempt %d/%d failed: %s", attempt, MAX_DB_RETRIES, error)
            if attempt < MAX_DB_RETRIES:
                time.sleep(2**attempt)
        finally:
            if connection is not None:
                connection.close()
    LOGGER.error("Event discarded after %d PostgreSQL attempts: %s", MAX_DB_RETRIES, event)
    return False


def format_event(event: dict[str, Any], scores: dict[int, list[int]]) -> str:
    """Format an event for readable operational logs."""
    match_id = int(event["match_id"])
    current_score = scores.setdefault(match_id, [0, 0])
    if event["event_type"] == "GOAL":
        if event["team"] == "Barcelona":
            current_score[0] += 1
        else:
            current_score[1] += 1
        return f"⚽ GOAL - {event['player']} ({event['minute']}') [Barça {current_score[0]}-{current_score[1]}]"
    labels = {"YELLOW_CARD": "🟨 YELLOW CARD", "RED_CARD": "🟥 RED CARD", "SUBSTITUTION": "🔄 SUBSTITUTION"}
    return f"{labels.get(event['event_type'], event['event_type'])} - {event['player']} ({event['minute']}') [{event['team']}]"


def run() -> None:
    """Consume events continuously until the process is stopped."""
    load_dotenv(REPOSITORY_ROOT / ".env")
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
    scores: dict[int, list[int]] = {}
    LOGGER.info("Listening to Kafka topic %s", TOPIC)
    try:
        for message in consumer:
            event = message.value
            if insert_event_with_retry(event):
                LOGGER.info(format_event(event, scores))
    except KeyboardInterrupt:
        LOGGER.info("Consumer stopped by user")
    except KafkaError as error:
        LOGGER.error("Kafka consuming failed: %s", error)
        raise
    finally:
        consumer.close()


if __name__ == "__main__":
    configure_logging()
    run()
