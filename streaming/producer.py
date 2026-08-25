"""Produce simulated live football events to Kafka."""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from kafka import KafkaProducer
from kafka.errors import KafkaError


LOGGER = logging.getLogger("barca_event_producer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOPIC = "barca-live-events"
DEFAULT_BOOTSTRAP_SERVERS = "localhost:9092"
EVENT_TYPES = ("GOAL", "YELLOW_CARD", "RED_CARD", "SUBSTITUTION")
PLAYERS = (
    ("Lewandowski", "Barcelona"),
    ("Lamine Yamal", "Barcelona"),
    ("Raphinha", "Barcelona"),
    ("Pedri", "Barcelona"),
    ("Ferran Torres", "Barcelona"),
    ("Vinicius Jr", "Real Madrid"),
    ("Bellingham", "Real Madrid"),
)


def configure_logging() -> None:
    """Configure logs for producer execution."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def get_live_match() -> dict[str, Any]:
    """Read one in-play match, or return a fictional Barcelona match."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    from load import get_connection

    try:
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT match_id, home_team, away_team, home_goals, away_goals
                    FROM matches
                    WHERE status = 'IN_PLAY'
                    ORDER BY match_id
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        if row:
            match = {
                "match_id": row[0],
                "home_team": row[1],
                "away_team": row[2],
                "home_goals": row[3] or 0,
                "away_goals": row[4] or 0,
            }
            LOGGER.info("Using live match %s: %s vs %s", match["match_id"], match["home_team"], match["away_team"])
            return match
    except Exception as error:
        LOGGER.warning("Could not query live matches; using simulation: %s", error)

    simulated_match = {
        "match_id": 999999,
        "home_team": "Barcelona",
        "away_team": "Real Madrid",
        "home_goals": 0,
        "away_goals": 0,
    }
    LOGGER.info("No IN_PLAY match found; using fictional match Barcelona vs Real Madrid")
    return simulated_match


def create_producer() -> KafkaProducer:
    """Create a JSON Kafka producer using the configured bootstrap server."""
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", DEFAULT_BOOTSTRAP_SERVERS).split(",")
    return KafkaProducer(
        bootstrap_servers=servers,
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8"),
        acks="all",
        retries=3,
    )


def build_event(match: dict[str, Any], minute: int) -> dict[str, Any]:
    """Build one random event for the current simulated minute."""
    event_type = random.choices(EVENT_TYPES, weights=(25, 45, 5, 25), k=1)[0]
    player, team = random.choice(PLAYERS)
    if event_type == "GOAL":
        match["home_goals" if team == match["home_team"] else "away_goals"] += 1
    return {
        "event_type": event_type,
        "minute": minute,
        "player": player,
        "team": team,
        "match_id": match["match_id"],
    }


def run() -> None:
    """Publish one event every 30 seconds until minute 90."""
    load_dotenv(REPOSITORY_ROOT / ".env")
    match = get_live_match()
    producer = create_producer()
    simulated_minute = 1
    try:
        while simulated_minute <= 90:
            event = build_event(match, simulated_minute)
            future = producer.send(TOPIC, value=event)
            future.get(timeout=10)
            LOGGER.info("Published %s at minute %s", event["event_type"], simulated_minute)
            simulated_minute += 1
            if simulated_minute <= 90:
                time.sleep(30)
        producer.flush()
        LOGGER.info("Simulated match finished at 90 minutes")
    except (KafkaError, TimeoutError) as error:
        LOGGER.error("Kafka publishing failed: %s", error)
        raise
    finally:
        producer.close()


if __name__ == "__main__":
    configure_logging()
    run()
