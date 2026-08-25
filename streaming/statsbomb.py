"""Read real match events from the StatsBomb open data set.

StatsBomb publishes event-level data for selected competitions for free. Their
La Liga collection covers the 2004/05 to 2020/21 seasons and every match in it
features Barcelona, which makes it the natural source for this project's
streaming layer.

Data source: StatsBomb Open Data (https://github.com/statsbomb/open-data).
Their licence requires that any published analysis credit StatsBomb as the
source; see the attribution section of the project README.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("statsbomb_open_data")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPOSITORY_ROOT / "data" / "raw" / "statsbomb"

OPEN_DATA_BASE_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
LA_LIGA_COMPETITION_ID = 11
DEFAULT_SEASON_ID = 90  # 2020/2021

# An event file holds every touch of the ball: a single match runs to roughly
# 3,800 events, of which more than half are passes and ball receipts. These are
# the ones a live match feed would actually broadcast.
NOTABLE_EVENT_TYPES = frozenset(
    {"GOAL", "OWN_GOAL", "SHOT", "YELLOW_CARD", "SECOND_YELLOW", "RED_CARD", "SUBSTITUTION", "PERIOD_START", "PERIOD_END"}
)

# StatsBomb card names mapped onto the vocabulary this pipeline publishes.
CARD_TYPES = {
    "Yellow Card": "YELLOW_CARD",
    "Second Yellow": "SECOND_YELLOW",
    "Red Card": "RED_CARD",
}


def _fetch_json(url: str, cache_path: Path) -> Any:
    """Return JSON from the local cache, downloading it once if it is absent.

    Event files are around 3 MB each, so they are cached under data/raw/ instead
    of being re-downloaded on every run, and they stay out of version control.
    """
    if cache_path.is_file():
        LOGGER.debug("Cache hit: %s", cache_path.name)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    LOGGER.info("Downloading %s", url)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "barca-data-pipeline"})
    with urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    LOGGER.info("Cached %s (%.1f MB)", cache_path.name, cache_path.stat().st_size / 1_048_576)
    return payload


def list_matches(season_id: int = DEFAULT_SEASON_ID) -> list[dict[str, Any]]:
    """Return the matches of one La Liga season, most recent first."""
    payload = _fetch_json(
        f"{OPEN_DATA_BASE_URL}/matches/{LA_LIGA_COMPETITION_ID}/{season_id}.json",
        CACHE_DIR / f"matches_{LA_LIGA_COMPETITION_ID}_{season_id}.json",
    )
    return sorted(payload, key=lambda match: match.get("match_date", ""), reverse=True)


def describe_match(match: dict[str, Any]) -> str:
    """Render one match as a single readable line."""
    return (
        f"{match['match_id']:>8}  {match.get('match_date', '?')}  "
        f"{match['home_team']['home_team_name']} {match.get('home_score', '?')}"
        f"-{match.get('away_score', '?')} {match['away_team']['away_team_name']}"
    )


def kickoff_datetime(match: dict[str, Any]) -> datetime:
    """Combine the match date and kick-off time into a single instant.

    StatsBomb reports kick_off as a local wall-clock time with no offset, so it
    is read as UTC. That keeps event timestamps ordered and comparable, which is
    what the pipeline needs; it is not a claim about the stadium's local time.
    """
    date_part = match.get("match_date", "1970-01-01")
    time_part = (match.get("kick_off") or "00:00:00.000").split(".")[0]
    return datetime.fromisoformat(f"{date_part}T{time_part}").replace(tzinfo=timezone.utc)


def _classify(raw: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map a StatsBomb event onto this pipeline's event type and detail payload.

    Returns None for event types the pipeline does not publish, which keeps the
    decision about what counts as an event in one place.
    """
    type_name = raw["type"]["name"]

    if type_name == "Shot":
        shot = raw.get("shot") or {}
        outcome = (shot.get("outcome") or {}).get("name")
        detail = {
            "outcome": outcome,
            "expected_goals": shot.get("statsbomb_xg"),
            "body_part": (shot.get("body_part") or {}).get("name"),
            "play_pattern": (raw.get("play_pattern") or {}).get("name"),
        }
        return ("GOAL" if outcome == "Goal" else "SHOT"), detail

    if type_name == "Own Goal Against":
        return "OWN_GOAL", {}

    # A card can arrive attached to a foul or on its own as bad behaviour, so
    # both carriers are checked rather than assuming one of them.
    for carrier in ("foul_committed", "bad_behaviour"):
        card_name = ((raw.get(carrier) or {}).get("card") or {}).get("name")
        if card_name and card_name in CARD_TYPES:
            return CARD_TYPES[card_name], {"reason": type_name}

    if type_name == "Substitution":
        substitution = raw.get("substitution") or {}
        replacement = substitution.get("replacement") or {}
        return "SUBSTITUTION", {
            "replacement_id": replacement.get("id"),
            "replacement_name": replacement.get("name"),
            "outcome": (substitution.get("outcome") or {}).get("name"),
        }

    if type_name == "Half Start":
        return "PERIOD_START", {}
    if type_name == "Half End":
        return "PERIOD_END", {}

    return None


def normalize_event(raw: dict[str, Any], match: dict[str, Any], kickoff: datetime) -> dict[str, Any] | None:
    """Convert one StatsBomb event into the message published to Kafka.

    Returns None when the event is not one the pipeline publishes.
    """
    classified = _classify(raw)
    if classified is None:
        return None
    event_type, detail = classified

    minute = raw.get("minute") or 0
    second = raw.get("second") or 0
    team = raw.get("team") or {}
    player = raw.get("player") or {}

    return {
        # StatsBomb assigns every event a UUID. Carrying it through makes the
        # message naturally idempotent: the consumer keys on it, so replaying
        # the same match cannot duplicate a row.
        "event_id": raw["id"],
        "match_id": match["match_id"],
        "period": raw.get("period"),
        "minute": minute,
        "second": second,
        "event_type": event_type,
        "player_id": player.get("id"),
        "player_name": player.get("name"),
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "detail": {key: value for key, value in detail.items() if value is not None},
        # Event time: when this happened on the pitch, reconstructed from the
        # kick-off and the match clock. Distinct from the processing time the
        # consumer records on arrival.
        "event_ts": (kickoff + timedelta(seconds=minute * 60 + second)).isoformat(),
    }


def load_events(match: dict[str, Any], notable_only: bool = True) -> list[dict[str, Any]]:
    """Return the normalized events of one match in the order they occurred.

    Ordering follows StatsBomb's own index rather than the match clock: the
    minute field overlaps across the half-time boundary, because first-half
    stoppage time can read 46 while the second half restarts at 45.
    """
    raw_events = _fetch_json(
        f"{OPEN_DATA_BASE_URL}/events/{match['match_id']}.json",
        CACHE_DIR / f"events_{match['match_id']}.json",
    )
    kickoff = kickoff_datetime(match)

    events = []
    for raw in sorted(raw_events, key=lambda event: event.get("index", 0)):
        event = normalize_event(raw, match, kickoff)
        if event is None:
            continue
        if notable_only and event["event_type"] not in NOTABLE_EVENT_TYPES:
            continue
        events.append(event)

    LOGGER.info("Prepared %d event(s) from %d raw record(s)", len(events), len(raw_events))
    return events


def find_match(match_id: int | None, season_id: int = DEFAULT_SEASON_ID) -> dict[str, Any]:
    """Return the requested match, or the most recent one of the season."""
    matches = list_matches(season_id)
    if match_id is None:
        return matches[0]
    for match in matches:
        if match["match_id"] == match_id:
            return match
    raise ValueError(f"Match {match_id} is not part of La Liga season {season_id}")
