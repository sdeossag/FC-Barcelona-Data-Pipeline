"""Unit tests for PostgreSQL load data-quality checks."""

import pandas as pd

from scripts.load import validate_quality


def test_matches_quality_passes_for_valid_rows():
    """A complete match row should pass all quality checks."""
    frame = pd.DataFrame([{
        "match_id": 1,
        "date": pd.Timestamp("2026-08-24", tz="UTC"),
        "home_team": "Barcelona",
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
        "home_team": None,
        "away_team": "Real Madrid",
        "home_goals": 2,
        "away_goals": 1,
    }])
    assert validate_quality(frame, "matches") is False


def test_standings_quality_rejects_negative_points():
    """Negative points must prevent loading standings."""
    frame = pd.DataFrame([{
        "position": 1,
        "team_name": "Barcelona",
        "points": -1,
    }])
    assert validate_quality(frame, "standings") is False


def test_quality_rejects_empty_dataframe():
    """An empty DataFrame must never be loaded."""
    assert validate_quality(pd.DataFrame(), "matches") is False
