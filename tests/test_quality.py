"""Unit tests for PostgreSQL load data-quality checks."""

import pandas as pd

from scripts.load import validate_quality


def test_matches_quality_passes_for_valid_rows():
    """A complete match row should pass all quality checks."""
    frame = pd.DataFrame([{
        "match_id": 1,
        "date": pd.Timestamp("2026-08-24", tz="UTC"),
        "home_team_id": 81,
        "home_team": "Barcelona",
        "away_team_id": 86,
        "away_team": "Real Madrid",
        "home_goals": 2,
        "away_goals": 1,
    }])
    assert validate_quality(frame, "matches") is True


def test_matches_quality_rejects_critical_nulls():
    """A missing team name must prevent loading."""
    frame = pd.DataFrame([{
        "match_id": 1,
        "date": pd.Timestamp("2026-08-24", tz="UTC"),
        "home_team_id": 81,
        "home_team": None,
        "away_team_id": 86,
        "away_team": "Real Madrid",
        "home_goals": 2,
        "away_goals": 1,
    }])
    assert validate_quality(frame, "matches") is False


def test_standings_quality_rejects_negative_points():
    """Negative points must prevent loading standings."""
    frame = pd.DataFrame([{
        "team_id": 81,
        "position": 1,
        "team_name": "Barcelona",
        "points": -1,
    }])
    assert validate_quality(frame, "standings") is False


def test_matches_quality_rejects_missing_team_id():
    """Identifier columns are part of the primary key, so a null one must block the load."""
    frame = pd.DataFrame([{
        "match_id": 1,
        "date": pd.Timestamp("2026-08-24", tz="UTC"),
        "home_team_id": None,
        "home_team": "Barcelona",
        "away_team_id": 86,
        "away_team": "Real Madrid",
        "home_goals": 2,
        "away_goals": 1,
    }])
    assert validate_quality(frame, "matches") is False


def test_standings_quality_rejects_missing_team_id():
    """A standings row without team_id cannot be keyed and must be rejected."""
    frame = pd.DataFrame([{
        "team_id": None,
        "position": 1,
        "team_name": "Barcelona",
        "points": 10,
    }])
    assert validate_quality(frame, "standings") is False


def test_quality_rejects_empty_dataframe():
    """An empty DataFrame must never be loaded."""
    assert validate_quality(pd.DataFrame(), "matches") is False
