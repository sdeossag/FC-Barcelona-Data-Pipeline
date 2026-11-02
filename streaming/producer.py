"""Publish real FC Barcelona match events from ESPN to Kafka.

Three modes share one pipeline:

  replay    publish a finished match, paced by its real match clock, divided by
            --speed. A full match streams in about ninety seconds at the default.
  backfill  publish every finished Barcelona match of the season without pacing,
            to load the season's history in one go.
  live      follow a match in progress: poll ESPN and publish each new event as
            it appears, until the match is over.

Replaying a recorded event log is a normal production practice, not a stand-in
for a live feed. It is how streaming pipelines are load tested, how a consumer
is re-run after a bad deploy, and how a bug is reproduced from the same input.
It also forces the pipeline to separate event time from processing time.

The data comes from ESPN's public, undocumented soccer API; see espn.py for what
that implies.

Examples:
    python streaming/producer.py --list-matches
    python streaming/producer.py                      # latest finished match
    python streaming/producer.py --match-id 401882822 --speed 60
    python streaming/producer.py --backfill
    python streaming/producer.py --live --wait
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

import espn


LOGGER = logging.getLogger("barca_event_producer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOPIC = "barca-live-events"
DEFAULT_BOOTSTRAP_SERVERS = "localhost:9092"
DEFAULT_SPEED = 60.0

# Half-time is a fifteen-minute hole in the match clock. Waiting it out in
# proportion would stall the replay for no benefit, so gaps are capped.
MAX_EVENT_GAP_SECONDS = 120

LIVE_POLL_SECONDS = 30
WAIT_FOR_KICKOFF_SECONDS = 300


def configure_logging() -> None:
    """Configure logs for producer execution."""
    # Windows consoles default to cp1252, which cannot render accented names.
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
    labels = {
        "GOAL": "GOAL       ", "OWN_GOAL": "OWN GOAL   ", "SHOT": "shot       ",
        "YELLOW_CARD": "yellow card", "RED_CARD": "RED CARD   ",
        "SUBSTITUTION": "substitute ", "PERIOD_START": "period start", "PERIOD_END": "period end ",
    }
    detail = event["detail"]
    extra = ""
    if event["event_type"] == "SHOT":
        extra = f" ({detail.get('outcome')})"
    elif event["event_type"] == "GOAL" and detail.get("assist_name"):
        extra = f" (assist {detail['assist_name']})"
    elif event["event_type"] == "SUBSTITUTION" and detail.get("replacement_name"):
        extra = f" -> {detail['replacement_name']}"
    who = event.get("player_name") or "-"
    label = labels.get(event["event_type"], event["event_type"])
    return f"{event['minute']:>3}'{event['second']:02d}  {label}  {who} [{event.get('team_name') or '-'}]{extra}"


def publish(producer: KafkaProducer, event: dict[str, Any]) -> None:
    """Publish one event and wait for the broker to acknowledge it."""
    # Stamp the moment of publication. Together with event_ts this lets the
    # warehouse compare when something happened against when it was handled.
    event["produced_at"] = datetime.now(timezone.utc).isoformat()
    producer.send(TOPIC, key=event["match_id"], value=event).get(timeout=10)
    LOGGER.info(format_event(event))


def _gap_seconds(previous: dict[str, Any], current: dict[str, Any]) -> float:
    """Real seconds between two events, clamped to [0, MAX_EVENT_GAP_SECONDS]."""
    if not previous["event_ts"] or not current["event_ts"]:
        return 0.0
    delta = (datetime.fromisoformat(current["event_ts"]) - datetime.fromisoformat(previous["event_ts"])).total_seconds()
    return min(max(delta, 0.0), MAX_EVENT_GAP_SECONDS)


def replay(events: list[dict[str, Any]], producer: KafkaProducer, speed: float | None) -> int:
    """Publish events in order, pacing them by their real timestamps / speed.

    With speed None the events go out back to back. Returns the number published.
    """
    previous = None
    for event in events:
        if speed and previous is not None:
            gap = _gap_seconds(previous, event)
            if gap:
                time.sleep(gap / speed)
        previous = event
        publish(producer, event)
    producer.flush()
    return len(events)


def follow_live(match: dict[str, Any], producer: KafkaProducer, poll_seconds: int) -> int:
    """Poll a match in progress and publish each new event once, until it ends."""
    published_ids: set[str] = set()
    published = 0
    while True:
        summary = espn.fetch_summary(match)
        fresh = [e for e in espn.load_events(match, summary) if e["event_id"] not in published_ids]
        for event in fresh:
            publish(producer, event)
            published_ids.add(event["event_id"])
            published += 1
        if espn.match_state(summary)["completed"]:
            LOGGER.info("Match finished: %d event(s) published", published)
            producer.flush()
            return published
        time.sleep(poll_seconds)


def find_live_match(wait: bool) -> dict[str, Any] | None:
    """Return a Barcelona match in progress, optionally waiting for kick-off."""
    while True:
        for match in espn.list_all_matches():
            if match["state"] == "in":
                return match
        if not wait:
            return None
        LOGGER.info("No Barcelona match in progress; checking again in %d s", WAIT_FOR_KICKOFF_SECONDS)
        time.sleep(WAIT_FOR_KICKOFF_SECONDS)


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--match-id", help="ESPN match id (default: the latest finished Barcelona match)")
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_SPEED,
        help=f"Replay multiplier; {DEFAULT_SPEED:.0f} streams a 90 minute match in about 90 seconds",
    )
    parser.add_argument("--backfill", action="store_true", help="Publish every finished match of the season, unpaced")
    parser.add_argument("--live", action="store_true", help="Follow a match in progress")
    parser.add_argument("--wait", action="store_true", help="With --live, wait for a match to kick off")
    parser.add_argument("--list-matches", action="store_true", help="List Barcelona's matches and exit")
    return parser.parse_args()


def main() -> int:
    """Run the producer from the command line."""
    configure_logging()
    load_dotenv(REPOSITORY_ROOT / ".env")
    args = parse_args()

    if args.speed <= 0:
        LOGGER.error("--speed must be greater than zero")
        return 2

    try:
        if args.list_matches:
            for match in espn.list_all_matches():
                print(espn.describe_match(match))
            return 0

        if args.live:
            match = find_live_match(args.wait)
            if match is None:
                LOGGER.info("No Barcelona match is in progress. Use --wait to wait for kick-off.")
                return 0
            LOGGER.info("Following %s", espn.describe_match(match).strip())
            matches, speed = [match], None
        elif args.backfill:
            # Oldest first, so the most recent match is the last one ingested
            # and the dashboard's "current match" is the latest game.
            finished = [m for m in espn.list_all_matches() if m["completed"]]
            matches, speed = sorted(finished, key=lambda m: m["date"]), None
            LOGGER.info("Backfilling %d finished match(es)", len(matches))
        else:
            matches, speed = [espn.find_match(args.match_id)], args.speed
            LOGGER.info("Replaying %s", espn.describe_match(matches[0]).strip())
    except (ValueError, OSError) as error:
        LOGGER.error("Could not load match: %s", error)
        return 1

    producer = create_producer()
    total = 0
    try:
        for match in matches:
            if args.live:
                total += follow_live(match, producer, LIVE_POLL_SECONDS)
            else:
                events = espn.load_events(match)
                if not events:
                    LOGGER.warning("No events for %s", match["name"])
                    continue
                total += replay(events, producer, speed)
        LOGGER.info("Done: %d event(s) published to %s", total, TOPIC)
    except (KafkaError, TimeoutError, OSError) as error:
        LOGGER.error("Publishing failed: %s", error)
        return 1
    finally:
        producer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
