"""Replay real Barcelona match events from StatsBomb open data into Kafka.

The events are genuine: real players, real minutes, real order of play. What the
producer controls is the pace at which they are published, so a full match can
be streamed in ninety seconds instead of ninety minutes.

Replaying a recorded event log is a normal production practice, not a stand-in
for a live feed. It is how streaming pipelines are load tested, how a consumer
is re-run after a bad deploy, and how the same input is used to reproduce a bug.
It also forces the pipeline to separate event time from processing time, which a
live feed lets you ignore.

Examples:
    python streaming/producer.py --list-matches
    python streaming/producer.py --match-id 3773565 --speed 60
    python streaming/producer.py --speed 1 --all-events
"""

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

from dotenv import load_dotenv
from kafka import KafkaProducer
from kafka.errors import KafkaError

import statsbomb


LOGGER = logging.getLogger("barca_event_producer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOPIC = "barca-live-events"
DEFAULT_BOOTSTRAP_SERVERS = "localhost:9092"
DEFAULT_SPEED = 60.0

# Half-time is a fifteen-minute hole in the match clock. Waiting it out in
# proportion would stall the replay for no benefit, so gaps are capped.
MAX_EVENT_GAP_SECONDS = 120


def configure_logging() -> None:
    """Configure logs for producer execution."""
    # Windows consoles default to cp1252, which cannot render the match emojis.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def create_producer() -> KafkaProducer:
    """Create a JSON Kafka producer using the configured bootstrap server."""
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", DEFAULT_BOOTSTRAP_SERVERS).split(",")
    return KafkaProducer(
        bootstrap_servers=servers,
        # The key decides the partition. Keying on match_id puts every event of
        # a match on one partition, and Kafka guarantees ordering within a
        # partition, so the consumer sees the match unfold in the right order.
        key_serializer=lambda key: str(key).encode("utf-8"),
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8"),
        # Wait for every in-sync replica before treating a send as successful.
        acks="all",
        retries=3,
    )


def format_event(event: dict[str, Any]) -> str:
    """Render one event as a readable line for operational logs."""
    icons = {
        "GOAL": "GOAL       ",
        "OWN_GOAL": "OWN GOAL   ",
        "SHOT": "shot       ",
        "YELLOW_CARD": "yellow card",
        "SECOND_YELLOW": "2nd yellow ",
        "RED_CARD": "RED CARD   ",
        "SUBSTITUTION": "substitute ",
        "PERIOD_START": "period start",
        "PERIOD_END": "period end ",
    }
    label = icons.get(event["event_type"], event["event_type"])
    who = event.get("player_name") or "-"
    extra = ""
    if event["event_type"] in ("GOAL", "SHOT") and event["detail"].get("expected_goals") is not None:
        extra = f" (xG {event['detail']['expected_goals']:.2f})"
    elif event["event_type"] == "SUBSTITUTION" and event["detail"].get("replacement_name"):
        extra = f" -> {event['detail']['replacement_name']}"
    return f"{event['minute']:>3}'{event['second']:02d}  {label}  {who} [{event.get('team_name') or '-'}]{extra}"


def replay(events: list[dict[str, Any]], producer: KafkaProducer, speed: float) -> int:
    """Publish events pacing them by the real match clock, divided by speed.

    Returns the number of events published.
    """
    published = 0
    previous_elapsed: int | None = None

    for event in events:
        elapsed = event["minute"] * 60 + event["second"]

        if previous_elapsed is not None:
            # Clamp at zero: the minute field overlaps at the half-time
            # boundary, so consecutive events can appear to move backwards.
            gap = min(max(elapsed - previous_elapsed, 0), MAX_EVENT_GAP_SECONDS)
            if gap:
                time.sleep(gap / speed)
        previous_elapsed = elapsed

        # Stamp the moment of publication. Together with event_ts this lets the
        # warehouse compare when something happened against when it was handled.
        event["produced_at"] = datetime.now(timezone.utc).isoformat()

        future = producer.send(TOPIC, key=event["match_id"], value=event)
        future.get(timeout=10)
        published += 1
        LOGGER.info(format_event(event))

    producer.flush()
    return published


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the replay."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--match-id", type=int, help="StatsBomb match id (default: most recent of the season)")
    parser.add_argument("--season-id", type=int, default=statsbomb.DEFAULT_SEASON_ID, help="La Liga season id")
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_SPEED,
        help=f"Replay multiplier; {DEFAULT_SPEED:.0f} streams a 90 minute match in about 90 seconds",
    )
    parser.add_argument("--all-events", action="store_true", help="Publish every touch, not only notable events")
    parser.add_argument("--list-matches", action="store_true", help="List available matches and exit")
    return parser.parse_args()


def main() -> int:
    """Run the replay from the command line."""
    configure_logging()
    load_dotenv(REPOSITORY_ROOT / ".env")
    args = parse_args()

    if args.speed <= 0:
        LOGGER.error("--speed must be greater than zero")
        return 2

    if args.list_matches:
        for match in statsbomb.list_matches(args.season_id):
            print(statsbomb.describe_match(match))
        return 0

    try:
        match = statsbomb.find_match(args.match_id, args.season_id)
    except (ValueError, OSError) as error:
        LOGGER.error("Could not load match: %s", error)
        return 1

    LOGGER.info("Replaying %s", statsbomb.describe_match(match).strip())
    events = statsbomb.load_events(match, notable_only=not args.all_events)
    if not events:
        LOGGER.error("No events to publish for match %s", match["match_id"])
        return 1

    match_minutes = (events[-1]["minute"] * 60 + events[-1]["second"]) / 60
    LOGGER.info(
        "Publishing %d event(s) at %.0fx: about %.0f seconds of wall clock",
        len(events), args.speed, match_minutes * 60 / args.speed,
    )

    producer = create_producer()
    try:
        published = replay(events, producer, args.speed)
        LOGGER.info("Replay finished: %d event(s) published to %s", published, TOPIC)
    except (KafkaError, TimeoutError) as error:
        LOGGER.error("Kafka publishing failed: %s", error)
        return 1
    finally:
        producer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
