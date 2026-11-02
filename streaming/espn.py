"""Read real Barcelona match events from ESPN's public soccer API.

ESPN serves the data behind its own match pages from JSON endpoints that need no
account or key. For a match they return key events (goals, cards, substitutions,
kick-off and half-time) and a play-by-play commentary that includes every shot,
each with the match minute, the player and the real wall-clock time.

This is an unofficial, undocumented API. ESPN publishes no contract for it, so
its shape can change without notice and it should not back anything critical.
That is acceptable for a portfolio pipeline, and it is the reason the parsing
below is defensive: a field that is missing is skipped, never assumed.

Why not football-data.org, which the batch layer uses? Its free tier returns
only the half-time and full-time score: no scorers, cards or minutes. The
StatsBomb open data the streaming layer used before stops at the 2020/21 season.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("espn_match_events")

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"
BARCELONA_TEAM_ID = "83"

# ESPN league slugs for the competitions the warehouse covers.
LEAGUES = {"esp.1": "La Liga", "uefa.champions": "Champions League"}

# Namespace for deriving event ids. ESPN's ids are short integers that are only
# unique inside ESPN, while live_events keys on a UUID. A name-based UUID (v5)
# is deterministic: the same ESPN play always yields the same event_id, which is
# what keeps the consumer idempotent when a match is replayed or re-polled.
EVENT_NAMESPACE = uuid.UUID("6f1c2a5e-0b1d-4a52-9a3e-5d6f0d3c9b10")

# Non-goal key events mapped onto the vocabulary this pipeline publishes. Goals
# are classified separately in _classify_key_event because ESPN labels them in
# several ways.
KEY_EVENT_TYPES = {
    "yellow card": "YELLOW_CARD",
    "red card": "RED_CARD",
    "substitution": "SUBSTITUTION",
    "kickoff": "PERIOD_START",
    "start 2nd half": "PERIOD_START",
    "halftime": "PERIOD_END",
    "end regular time": "PERIOD_END",
}

# Commentary lines that describe a shot, and how each one ended.
SHOT_OUTCOMES = {
    "shot on target": "On target",
    "shot off target": "Off target",
    "shot blocked": "Blocked",
    "shot hit woodwork": "Woodwork",
}

NOTABLE_EVENT_TYPES = frozenset(KEY_EVENT_TYPES.values()) | {"GOAL", "OWN_GOAL", "SHOT"}


def fetch_json(url: str) -> Any:
    """GET one JSON document."""
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "barca-data-pipeline"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def list_matches(league: str, team_id: str = BARCELONA_TEAM_ID) -> list[dict[str, Any]]:
    """Return a team's matches in one competition, most recent first."""
    payload = fetch_json(f"{BASE_URL}/{league}/teams/{team_id}/schedule")
    matches = []
    for event in payload.get("events", []):
        status = (event.get("competitions") or [{}])[0].get("status", {}).get("type", {})
        matches.append({
            "event_id": str(event["id"]),
            "league": league,
            "date": event.get("date", ""),
            "name": event.get("name", ""),
            "state": status.get("state", "pre"),
            "completed": bool(status.get("completed")),
        })
    return sorted(matches, key=lambda match: match["date"], reverse=True)


def list_all_matches() -> list[dict[str, Any]]:
    """Return Barcelona's matches across every covered competition, newest first."""
    matches = []
    for league in LEAGUES:
        try:
            matches.extend(list_matches(league))
        except OSError as error:
            LOGGER.warning("Could not read the %s schedule: %s", league, error)
    return sorted(matches, key=lambda match: match["date"], reverse=True)


def describe_match(match: dict[str, Any]) -> str:
    """Render one match as a single readable line."""
    return f"{match['event_id']:>10}  {match['date'][:10]}  {LEAGUES.get(match['league'], match['league']):<16}  {match['name']}  [{match['state']}]"


def find_match(event_id: str | None) -> dict[str, Any]:
    """Return the requested match, or Barcelona's most recent finished one."""
    matches = list_all_matches()
    if event_id is None:
        for match in matches:
            if match["completed"]:
                return match
        raise ValueError("No finished Barcelona match found")
    for match in matches:
        if match["event_id"] == str(event_id):
            return match
    raise ValueError(f"Match {event_id} is not in Barcelona's schedule")


def fetch_summary(match: dict[str, Any]) -> dict[str, Any]:
    """Return ESPN's full summary document for one match."""
    return fetch_json(f"{BASE_URL}/{match['league']}/summary?event={match['event_id']}")


def match_state(summary: dict[str, Any]) -> dict[str, Any]:
    """Return the match status, e.g. {'state': 'in', 'completed': False}."""
    competition = (summary.get("header", {}).get("competitions") or [{}])[0]
    status = competition.get("status", {}).get("type", {})
    return {"state": status.get("state", "pre"), "completed": bool(status.get("completed"))}


def _event_id(match: dict[str, Any], play_id: str) -> str:
    return str(uuid.uuid5(EVENT_NAMESPACE, f"espn:{match['league']}:{match['event_id']}:{play_id}"))


def _participants(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [p.get("athlete") or {} for p in raw.get("participants") or []]


def _wallclock(raw: dict[str, Any]) -> str | None:
    """ESPN's real wall-clock time for the play, as an ISO timestamp."""
    value = raw.get("wallclock")
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _team_ids(summary: dict[str, Any]) -> dict[str, str]:
    """Team id by display name. Commentary names the team but carries no id."""
    competition = (summary.get("header", {}).get("competitions") or [{}])[0]
    return {
        (c.get("team") or {}).get("displayName", ""): str((c.get("team") or {}).get("id", ""))
        for c in competition.get("competitors", [])
    }


def normalize_play(raw: dict[str, Any], match: dict[str, Any], event_type: str,
                   team_ids: dict[str, str], detail: dict[str, Any]) -> dict[str, Any]:
    """Build the message published to Kafka from one ESPN play."""
    clock = (raw.get("clock") or raw.get("time") or {}).get("value") or 0
    team = raw.get("team") or {}
    team_name = team.get("displayName")
    team_id = team.get("id") or team_ids.get(team_name or "")
    people = _participants(raw)

    player = people[0] if people else {}
    if event_type == "SUBSTITUTION" and len(people) > 1:
        # ESPN lists the player coming on first. The warehouse follows the
        # convention of the previous source: the player is the one leaving, and
        # the replacement travels in the detail payload.
        detail["replacement_name"] = people[0].get("displayName")
        detail["replacement_id"] = _to_int(people[0].get("id"))
        player = people[1]
    elif event_type == "GOAL" and len(people) > 1:
        detail["assist_name"] = people[1].get("displayName")

    detail.update({"source": "ESPN", "league": match["league"], "text": raw.get("text")})
    return {
        "event_id": _event_id(match, str(raw["id"])),
        "match_id": int(match["event_id"]),
        "period": (raw.get("period") or {}).get("number"),
        "minute": int(clock // 60),
        "second": int(clock % 60),
        "event_type": event_type,
        "player_id": _to_int(player.get("id")),
        "player_name": player.get("displayName"),
        "team_id": _to_int(team_id),
        "team_name": team_name,
        "detail": {key: value for key, value in detail.items() if value is not None},
        # The real instant, not a reconstruction from the match clock.
        "event_ts": _wallclock(raw),
    }


def _classify_key_event(raw: dict[str, Any]) -> str | None:
    """Map an ESPN key event onto this pipeline's event type.

    ESPN does not use one label for goals: it writes "Goal", "Goal - Header",
    "Goal - Free-kick", "Penalty - Scored" and "Own Goal". Matching only the
    plain "Goal" silently dropped about a third of the season's goals, which a
    comparison against the final scores in the batch layer exposed. The
    scoringPlay flag is the reliable signal, so it takes precedence.
    """
    type_name = ((raw.get("type") or {}).get("text") or "").lower()
    if type_name.startswith("own goal"):
        return "OWN_GOAL"
    if raw.get("scoringPlay") or type_name.startswith(("goal", "penalty - scored")):
        return "GOAL"
    return KEY_EVENT_TYPES.get(type_name)


def load_events(match: dict[str, Any], summary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the notable events of one match in the order they happened.

    Goals, cards, substitutions and period boundaries come from ESPN's key
    events. Shots come from the commentary, because the key events omit them.
    """
    summary = summary or fetch_summary(match)
    team_ids = _team_ids(summary)

    events: dict[str, dict[str, Any]] = {}
    for raw in summary.get("keyEvents") or []:
        event_type = _classify_key_event(raw)
        if event_type and raw.get("id") is not None:
            event = normalize_play(raw, match, event_type, team_ids, {})
            events[event["event_id"]] = event

    for entry in summary.get("commentary") or []:
        raw = entry.get("play") or {}
        outcome = SHOT_OUTCOMES.get(((raw.get("type") or {}).get("text") or "").lower())
        if outcome and raw.get("id") is not None:
            event = normalize_play(raw, match, "SHOT", team_ids, {"outcome": outcome})
            events.setdefault(event["event_id"], event)

    # Real wall-clock order. The match clock restarts at the second half and
    # repeats during stoppage time, so it cannot order events on its own.
    ordered = sorted(events.values(), key=lambda e: (e["event_ts"] or "", e["minute"], e["second"]))
    LOGGER.info("Prepared %d event(s) for %s", len(ordered), match["name"])
    return ordered
