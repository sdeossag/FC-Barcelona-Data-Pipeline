"""Unit tests for the ESPN event mapping. They run offline against a small summary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streaming"))

import espn  # noqa: E402


MATCH = {"event_id": "401882822", "league": "esp.1", "name": "Alaves at Barcelona"}


def _key_event(play_id, type_text, clock, text, team, people, period=1, scoring=False, wallclock="2026-10-31T20:10:00Z"):
    return {
        "id": play_id,
        "type": {"text": type_text},
        "text": text,
        "period": {"number": period},
        "clock": {"value": clock},
        "scoringPlay": scoring,
        "team": {"id": team[0], "displayName": team[1]},
        "participants": [{"athlete": {"id": i, "displayName": n}} for i, n in people],
        "wallclock": wallclock,
    }


SUMMARY = {
    "header": {"competitions": [{"competitors": [
        {"team": {"id": "83", "displayName": "Barcelona"}},
        {"team": {"id": "96", "displayName": "Alaves"}},
    ]}]},
    "keyEvents": [
        _key_event("1", "Goal", 480, "Goal! Barcelona 1, Alaves 0.", ("83", "Barcelona"),
                   [("10", "Raphinha"), ("11", "Fermin")], scoring=True),
        # ESPN labels some goals with a subtype; all of them must count as goals.
        _key_event("2", "Goal - Header", 1200, "Goal! Barcelona 2, Alaves 0.", ("83", "Barcelona"),
                   [("12", "Lewandowski")], scoring=True, wallclock="2026-10-31T20:20:00Z"),
        _key_event("3", "Goal - Free-kick", 2000, "Goal! Barcelona 3, Alaves 0.", ("83", "Barcelona"),
                   [("13", "Yamal")], scoring=True, wallclock="2026-10-31T20:30:00Z"),
        _key_event("4", "Penalty - Scored", 3000, "Goal! Barcelona 4, Alaves 0.", ("83", "Barcelona"),
                   [("14", "Pedri")], period=2, scoring=True, wallclock="2026-10-31T20:50:00Z"),
        _key_event("5", "Own Goal", 3300, "Own Goal by Someone, Alaves. Barcelona 5, Alaves 0.", ("83", "Barcelona"),
                   [("99", "Someone")], period=2, scoring=True, wallclock="2026-10-31T20:55:00Z"),
        _key_event("6", "Yellow Card", 1500, "Pedri is shown the yellow card.", ("83", "Barcelona"),
                   [("14", "Pedri")], wallclock="2026-10-31T20:25:00Z"),
        _key_event("7", "Substitution", 2800, "Substitution, Barcelona. Bernal replaces Pedri.", ("83", "Barcelona"),
                   [("20", "Bernal"), ("14", "Pedri")], period=2, wallclock="2026-10-31T20:46:00Z"),
        _key_event("8", "Start Delay", 100, "Delay", ("83", "Barcelona"), []),
    ],
    "commentary": [
        {"play": {"id": "9", "type": {"text": "Shot On Target"}, "text": "Attempt saved.",
                  "period": {"number": 1}, "clock": {"value": 600},
                  "team": {"displayName": "Alaves"}, "participants": [{"athlete": {"displayName": "Rival"}}],
                  "wallclock": "2026-10-31T20:12:00Z"}},
        {"play": {"id": "10", "type": {"text": "Foul"}, "text": "Foul by someone.",
                  "period": {"number": 1}, "clock": {"value": 700}, "wallclock": "2026-10-31T20:13:00Z"}},
    ],
}


def _events():
    return espn.load_events(MATCH, SUMMARY)


def test_every_goal_label_counts_as_a_goal():
    types = [e["event_type"] for e in _events()]
    assert types.count("GOAL") == 4
    assert types.count("OWN_GOAL") == 1


def test_goal_carries_the_assist():
    first_goal = next(e for e in _events() if e["detail"].get("assist_name"))
    assert first_goal["player_name"] == "Raphinha"
    assert first_goal["detail"]["assist_name"] == "Fermin"


def test_substitution_records_the_player_leaving_and_the_replacement():
    sub = next(e for e in _events() if e["event_type"] == "SUBSTITUTION")
    assert sub["player_name"] == "Pedri"
    assert sub["detail"]["replacement_name"] == "Bernal"


def test_shots_come_from_commentary_and_take_the_team_id_from_the_header():
    shot = next(e for e in _events() if e["event_type"] == "SHOT")
    assert shot["detail"]["outcome"] == "On target"
    assert shot["team_id"] == 96


def test_unrelated_plays_are_not_published():
    types = {e["event_type"] for e in _events()}
    assert types <= espn.NOTABLE_EVENT_TYPES
    assert len(_events()) == 8  # 5 goals, 1 card, 1 substitution, 1 shot


def test_minute_and_second_come_from_the_match_clock():
    goal = next(e for e in _events() if e["player_name"] == "Raphinha")
    assert (goal["minute"], goal["second"]) == (8, 0)


def test_events_are_ordered_by_wall_clock():
    stamps = [e["event_ts"] for e in _events()]
    assert stamps == sorted(stamps)


def test_event_ids_are_deterministic_so_a_replay_cannot_duplicate_rows():
    first = [e["event_id"] for e in _events()]
    second = [e["event_id"] for e in _events()]
    assert first == second
    assert len(set(first)) == len(first)
