"""Unit tests for the match_predictions row mapping.

The classifier emits probabilities in its own label order (away, draw, home).
Mapping them to the wrong columns still satisfies the database's sum-to-one
constraint, so only a direct test catches it.
"""

import numpy as np
import pandas as pd

from scripts.predict import _prediction_rows


FIXTURES = pd.DataFrame([{
    "match_id": 1,
    "season": 2026,
    "match_date": pd.Timestamp("2026-11-01", tz="UTC"),
    "home_team_id": 81,
    "home_team": "FC Barcelona",
    "away_team_id": 263,
    "away_team": "Deportivo Alavés",
}])


def test_probabilities_follow_the_classifier_label_order():
    """Index 0 is away, 1 is draw and 2 is home."""
    probabilities = np.array([[0.10, 0.20, 0.70]])

    row = _prediction_rows(FIXTURES, probabilities, "PD", "match_outcome_lr_v1")[0]

    # (match_id, competition, season, date, home_id, home, away_id, away, ...)
    assert row[8:11] == (0.70, 0.20, 0.10)
    assert row[11] == "H"
    assert row[12] == 0.70


def test_away_favourite_is_labelled_away():
    """A high away probability must produce an A, not an H."""
    probabilities = np.array([[0.80, 0.10, 0.10]])

    row = _prediction_rows(FIXTURES, probabilities, "PD", "match_outcome_lr_v1")[0]

    assert row[11] == "A"
    assert row[12] == 0.80
