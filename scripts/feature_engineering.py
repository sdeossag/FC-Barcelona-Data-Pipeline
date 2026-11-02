"""Build the pre-match feature matrix the prediction models train on.

Every feature here answers the same question: what was knowable before kick-off?
A feature that peeks at the final score inflates test accuracy and then collapses
in production, and it is the single easiest mistake to make in sports modelling.

The defence against it is structural rather than a review checklist. All features
are produced by ONE forward pass over the matches in chronological order, and
within each match the order is fixed: read the accumulated state, emit the row,
and only then fold the result in. A future match cannot influence a past row
because, at the moment the row is written, the future has not been read yet.

Ratings are kept per competition. A side can dominate its domestic league and
struggle in Europe, and a single blended rating would describe neither.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd
from psycopg2.extras import execute_values

from db import get_connection


LOGGER = logging.getLogger("football_feature_engineering")

# Elo parameters. K controls how sharply a rating reacts to one result: 32 is the
# usual choice for football, high enough to track form over a season and low
# enough that a single upset does not rewrite a team's standing.
INITIAL_ELO = 1500.0
K_FACTOR = 32.0

# A 400-point gap corresponds to roughly a 10:1 expected-score ratio. This is the
# constant that defines the Elo scale, not a tunable.
ELO_SCALE = 400.0

FORM_WINDOW = 5
H2H_WINDOW = 5

# Raw stage values are not comparable across competitions or eras: La Liga says
# REGULAR_SEASON, the Champions League said GROUP_STAGE until 2023/24 and says
# LEAGUE_STAGE from 2024/25, when the group phase was replaced by a single table.
# Collapsing them here means the model sees "a league match" as one concept.
PHASE_BY_STAGE = {
    "REGULAR_SEASON": "league",
    "LEAGUE_STAGE": "league",
    "GROUP_STAGE": "group",
    "PLAYOFFS": "playoff",
    "LAST_16": "r16",
    "QUARTER_FINALS": "qf",
    "SEMI_FINALS": "sf",
    "FINAL": "final",
}

# Only the final is played on neutral ground; every other tie has a real host.
NEUTRAL_VENUE_PHASES = frozenset({"final"})

SELECT_MATCHES = """
    SELECT match_id, season, competition, date, stage,
           home_team_id, home_team, away_team_id, away_team,
           home_goals, away_goals, status
    FROM matches
    WHERE competition = %s
    ORDER BY date, match_id
"""

UPSERT_FEATURES = """
    INSERT INTO ml_features (
        match_id, season, competition, match_date,
        home_team_id, home_team, away_team_id, away_team,
        home_elo, away_elo, elo_difference,
        home_form_gf, home_form_ga, away_form_gf, away_form_ga,
        home_form_points, away_form_points, home_form_matches, away_form_matches,
        h2h_home_wins, h2h_draws, h2h_away_wins, h2h_home_gf, h2h_away_gf, h2h_matches,
        competition_phase, is_neutral_venue, result
    ) VALUES %s
    ON CONFLICT (match_id) DO UPDATE SET
        home_elo = EXCLUDED.home_elo, away_elo = EXCLUDED.away_elo,
        elo_difference = EXCLUDED.elo_difference,
        home_form_gf = EXCLUDED.home_form_gf, home_form_ga = EXCLUDED.home_form_ga,
        away_form_gf = EXCLUDED.away_form_gf, away_form_ga = EXCLUDED.away_form_ga,
        home_form_points = EXCLUDED.home_form_points, away_form_points = EXCLUDED.away_form_points,
        home_form_matches = EXCLUDED.home_form_matches, away_form_matches = EXCLUDED.away_form_matches,
        h2h_home_wins = EXCLUDED.h2h_home_wins, h2h_draws = EXCLUDED.h2h_draws,
        h2h_away_wins = EXCLUDED.h2h_away_wins, h2h_home_gf = EXCLUDED.h2h_home_gf,
        h2h_away_gf = EXCLUDED.h2h_away_gf, h2h_matches = EXCLUDED.h2h_matches,
        competition_phase = EXCLUDED.competition_phase,
        is_neutral_venue = EXCLUDED.is_neutral_venue,
        result = EXCLUDED.result,
        computed_at = NOW()
"""

UPSERT_ELO_HISTORY = """
    INSERT INTO team_elo_history (
        team_id, team_name, competition, after_match_id, after_date, season, elo_before, elo_after
    ) VALUES %s
    ON CONFLICT (team_id, competition, after_match_id) DO UPDATE SET
        elo_before = EXCLUDED.elo_before,
        elo_after = EXCLUDED.elo_after
"""


def configure_logging() -> None:
    """Configure timestamped logs for command-line and Airflow execution."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


@dataclass
class FormSummary:
    """A team's record over its most recent matches, before the current one."""

    goals_for: float
    goals_against: float
    points: float
    matches: int


def expected_score(rating: float, opponent_rating: float) -> float:
    """Return the score Elo expects, between 0 and 1.

    A draw counts as half a win, so 0.5 means evenly matched. The logistic shape
    is what makes beating a stronger side worth more than beating a weaker one.
    """
    return 1.0 / (1.0 + 10.0 ** ((opponent_rating - rating) / ELO_SCALE))


def outcome_scores(home_goals: int, away_goals: int) -> tuple[float, float, str]:
    """Return the Elo scores for both sides and the categorical result."""
    if home_goals > away_goals:
        return 1.0, 0.0, "H"
    if home_goals < away_goals:
        return 0.0, 1.0, "A"
    return 0.5, 0.5, "D"


def summarise_form(history: Iterable[tuple[int, int, int]]) -> FormSummary:
    """Average a team's recent matches.

    Returns zeros when a team has no history yet rather than imputing a league
    average: a promoted side genuinely has no recent record, and inventing one
    would tell the model something that is not true. The companion `matches`
    count lets the model distinguish "no history" from "a poor run".
    """
    entries = list(history)
    if not entries:
        return FormSummary(0.0, 0.0, 0.0, 0)

    count = len(entries)
    return FormSummary(
        goals_for=sum(entry[0] for entry in entries) / count,
        goals_against=sum(entry[1] for entry in entries) / count,
        points=float(sum(entry[2] for entry in entries)),
        matches=count,
    )


def summarise_head_to_head(
    meetings: Iterable[tuple[int, int, int]], home_team_id: int
) -> dict[str, float]:
    """Summarise past meetings from the current home side's point of view.

    Each stored meeting records who hosted it, so a fixture played at the other
    ground still counts towards the same team's record. Reading the raw rows
    without that flip would credit the wins to whichever side happened to be at
    home back then.
    """
    wins = draws = losses = 0
    goals_for = goals_against = 0

    for past_home_id, past_home_goals, past_away_goals in meetings:
        if past_home_id == home_team_id:
            scored, conceded = past_home_goals, past_away_goals
        else:
            scored, conceded = past_away_goals, past_home_goals

        goals_for += scored
        goals_against += conceded
        if scored > conceded:
            wins += 1
        elif scored == conceded:
            draws += 1
        else:
            losses += 1

    played = wins + draws + losses
    return {
        "h2h_home_wins": wins,
        "h2h_draws": draws,
        "h2h_away_wins": losses,
        "h2h_home_gf": goals_for / played if played else 0.0,
        "h2h_away_gf": goals_against / played if played else 0.0,
        "h2h_matches": played,
    }


def build_features(matches: pd.DataFrame, competition: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk the season chronologically, emitting one feature row per match.

    Returns the feature matrix and the Elo history. The single pass is what makes
    leakage impossible: state is read and written to the row before the match's
    own result is folded back in, so no row can see its own outcome or any that
    followed it.
    """
    elo: dict[int, float] = defaultdict(lambda: INITIAL_ELO)
    form: dict[int, deque[tuple[int, int, int]]] = defaultdict(lambda: deque(maxlen=FORM_WINDOW))
    head_to_head: dict[frozenset[int], deque[tuple[int, int, int]]] = defaultdict(
        lambda: deque(maxlen=H2H_WINDOW)
    )

    feature_rows: list[dict[str, Any]] = []
    elo_rows: list[dict[str, Any]] = []
    unmapped_stages: set[str] = set()

    for match in matches.itertuples(index=False):
        home_id, away_id = int(match.home_team_id), int(match.away_team_id)

        # --- READ the state as it stands before this match -------------------
        home_elo, away_elo = elo[home_id], elo[away_id]
        home_form = summarise_form(form[home_id])
        away_form = summarise_form(form[away_id])
        h2h = summarise_head_to_head(head_to_head[frozenset((home_id, away_id))], home_id)

        stage = match.stage or ""
        phase = PHASE_BY_STAGE.get(stage)
        if phase is None:
            unmapped_stages.add(stage)
            # An unrecognised stage is treated as a league fixture rather than
            # dropped: losing a match would silently shrink the training set.
            phase = "league"

        played = match.status == "FINISHED"
        result = None
        if played:
            home_score, away_score, result = outcome_scores(int(match.home_goals), int(match.away_goals))

        # --- EMIT the row, using only what was read above --------------------
        feature_rows.append({
            "match_id": int(match.match_id),
            "season": int(match.season),
            "competition": competition,
            "match_date": match.date,
            "home_team_id": home_id,
            "home_team": match.home_team,
            "away_team_id": away_id,
            "away_team": match.away_team,
            "home_elo": home_elo,
            "away_elo": away_elo,
            "elo_difference": home_elo - away_elo,
            "home_form_gf": home_form.goals_for,
            "home_form_ga": home_form.goals_against,
            "away_form_gf": away_form.goals_for,
            "away_form_ga": away_form.goals_against,
            "home_form_points": home_form.points,
            "away_form_points": away_form.points,
            "home_form_matches": home_form.matches,
            "away_form_matches": away_form.matches,
            **h2h,
            "competition_phase": phase,
            "is_neutral_venue": phase in NEUTRAL_VENUE_PHASES,
            "result": result,
        })

        # --- UPDATE the state, only for matches that have been played --------
        # A scheduled fixture has no outcome to learn from. Folding one in would
        # also corrupt every later row, since the pass never looks back.
        if not played:
            continue

        home_goals, away_goals = int(match.home_goals), int(match.away_goals)

        new_home_elo = home_elo + K_FACTOR * (home_score - expected_score(home_elo, away_elo))
        new_away_elo = away_elo + K_FACTOR * (away_score - expected_score(away_elo, home_elo))
        elo[home_id], elo[away_id] = new_home_elo, new_away_elo

        for team_id, team_name, before, after in (
            (home_id, match.home_team, home_elo, new_home_elo),
            (away_id, match.away_team, away_elo, new_away_elo),
        ):
            elo_rows.append({
                "team_id": team_id,
                "team_name": team_name,
                "competition": competition,
                "after_match_id": int(match.match_id),
                "after_date": match.date,
                "season": int(match.season),
                "elo_before": before,
                "elo_after": after,
            })

        form[home_id].append((home_goals, away_goals, int(home_score * 3) if home_score != 0.5 else 1))
        form[away_id].append((away_goals, home_goals, int(away_score * 3) if away_score != 0.5 else 1))
        head_to_head[frozenset((home_id, away_id))].append((home_id, home_goals, away_goals))

    if unmapped_stages:
        LOGGER.warning("Unrecognised stage values mapped to 'league': %s", sorted(unmapped_stages))

    return pd.DataFrame(feature_rows), pd.DataFrame(elo_rows)


def load_matches(connection, competition: str) -> pd.DataFrame:
    """Read every match of one competition, oldest first."""
    with connection.cursor() as cursor:
        cursor.execute(SELECT_MATCHES, (competition,))
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns)


def _as_tuples(frame: pd.DataFrame, columns: list[str]) -> list[tuple]:
    """Convert a frame to plain Python tuples psycopg2 can adapt."""
    return [
        tuple(None if pd.isna(value) else (value.item() if hasattr(value, "item") else value)
              for value in row)
        for row in frame[columns].itertuples(index=False, name=None)
    ]


def save_features(connection, features: pd.DataFrame, elo_history: pd.DataFrame) -> None:
    """Persist the feature matrix and the Elo history."""
    feature_columns = [
        "match_id", "season", "competition", "match_date",
        "home_team_id", "home_team", "away_team_id", "away_team",
        "home_elo", "away_elo", "elo_difference",
        "home_form_gf", "home_form_ga", "away_form_gf", "away_form_ga",
        "home_form_points", "away_form_points", "home_form_matches", "away_form_matches",
        "h2h_home_wins", "h2h_draws", "h2h_away_wins", "h2h_home_gf", "h2h_away_gf", "h2h_matches",
        "competition_phase", "is_neutral_venue", "result",
    ]
    elo_columns = [
        "team_id", "team_name", "competition", "after_match_id", "after_date",
        "season", "elo_before", "elo_after",
    ]

    with connection:
        with connection.cursor() as cursor:
            execute_values(cursor, UPSERT_FEATURES, _as_tuples(features, feature_columns), page_size=500)
            if not elo_history.empty:
                execute_values(cursor, UPSERT_ELO_HISTORY, _as_tuples(elo_history, elo_columns), page_size=500)


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competitions", nargs="+", default=["PD", "CL"], help="Competition codes to process")
    return parser.parse_args()


def main() -> int:
    """Build and store the feature matrix for every requested competition."""
    configure_logging()
    args = parse_args()

    connection = get_connection()
    try:
        for competition in args.competitions:
            matches = load_matches(connection, competition)
            if matches.empty:
                LOGGER.warning("%s: no matches found, skipping", competition)
                continue

            features, elo_history = build_features(matches, competition)
            save_features(connection, features, elo_history)

            training = int(features["result"].notna().sum())
            pending = len(features) - training
            LOGGER.info(
                "%s: %d training sample(s), %d future match(es), %d Elo update(s)",
                competition, training, pending, len(elo_history),
            )
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
