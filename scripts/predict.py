"""Simulate the rest of the season and store the resulting probabilities.

Three models cooperate, each doing what it is best at:

  * The trained classifier supplies match outcome probabilities. It is
    calibrated on real results, so it decides how points are won.
  * A Poisson model supplies scorelines. Outcomes alone cannot settle a
    two-legged knockout tie, which is decided on aggregate goals.
  * Monte Carlo turns per-match probabilities into a season. Probability of
    winning a league is not a property of any single match; it emerges from
    thousands of complete seasons played out.

Two assumptions are made explicitly rather than buried.

Ratings are frozen. Each remaining fixture is scored once, with Elo and form as
they stand today, and those probabilities are held fixed across every simulated
season. A team that goes on a simulated ten-match run does not become stronger
within that simulation. Modelling that feedback would mean recomputing features
inside the loop, which is a different and far more expensive program; holding
them fixed slightly understates how extreme the tails should be.

Knockout draws are random among eligible teams. UEFA's seeding and country
protections are not modelled, so a specific bracket is not predicted -- only how
often each side survives it.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from psycopg2.extras import execute_values

import train_model as tm
from db import get_connection


LOGGER = logging.getLogger("football_predictions")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPOSITORY_ROOT / "models"

BARCELONA_TEAM_ID = 81
TARGET_SEASON = 2026
DEFAULT_SIMULATIONS = 10_000

# Encoded outcomes, matching the classifier's label order.
AWAY_WIN, DRAW, HOME_WIN = 0, 1, 2

# A home side scores about 10% more than the same side away. Applied to the
# Poisson rate, not to the classifier, which already learned it from the data.
HOME_ADVANTAGE = 1.10

# How many matches of evidence the prior is worth when projecting a player. At
# ten, a player's own record overtakes the prior around the midpoint of a
# season, which is roughly when a scoring rate stops being noise.
PRIOR_MATCH_WEIGHT = 10

# The same idea applied to team scoring rates. One Champions League league phase
# is eight matches, so ten matches of prior keeps a single strong campaign from
# being read as permanent superiority.
POISSON_PRIOR_MATCHES = 10

# Champions League league-phase cut-offs under the format introduced in 2024/25.
CL_DIRECT_QUALIFIERS = 8    # places 1-8 go straight to the round of 16
CL_PLAYOFF_PLACES = 24      # places 9-24 contest a play-off round


# ============================================================
# Poisson scoring rates
# ============================================================

def calculate_poisson_stats(connection, competition: str, seasons_used: int = 3) -> pd.DataFrame:
    """Derive attack and defence strengths relative to the competition average.

    A strength of 1.5 means "scores half again as many goals as a typical side
    in this competition". Expressing it relative to the average is what makes
    the number portable: La Liga and the Champions League score at different
    rates, and an absolute goals-per-match figure would not transfer.

    Only the most recent seasons are used. A side's rate three years ago says
    little about its current squad.
    """
    query = """
        SELECT team_id, team_name, season,
               COUNT(*)           AS matches_played,
               SUM(goals_for)     AS goals_for,
               SUM(goals_against) AS goals_against
        FROM vw_team_match_results
        WHERE competition = %s AND season >= %s
        GROUP BY team_id, team_name, season
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (competition, TARGET_SEASON - seasons_used))
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        LOGGER.warning("%s: no recent matches, cannot calibrate Poisson rates", competition)
        return frame

    totals = frame.groupby(["team_id", "team_name"], as_index=False).agg(
        matches_played=("matches_played", "sum"),
        goals_for=("goals_for", "sum"),
        goals_against=("goals_against", "sum"),
    )
    totals["avg_goals_scored"] = totals["goals_for"] / totals["matches_played"]
    totals["avg_goals_conceded"] = totals["goals_against"] / totals["matches_played"]

    competition_average = totals["goals_for"].sum() / totals["matches_played"].sum()

    # Shrink each rate toward the competition average in proportion to how
    # little the team has played. A side with six Champions League matches can
    # show 2.0 goals per match on a good run, and taken at face value that makes
    # it the strongest attack in Europe. The knockout simulation then compounds
    # the error across four two-legged ties, which is how a team with the third
    # best rating came to win the tournament three times as often as the best.
    #
    # The prior is the competition average itself, worth POISSON_PRIOR_MATCHES
    # matches of evidence. A team with a full league phase behind it is barely
    # moved; a team with six matches is pulled most of the way back.
    prior = POISSON_PRIOR_MATCHES * competition_average
    denominator = totals["matches_played"] + POISSON_PRIOR_MATCHES
    totals["attack_strength"] = ((totals["goals_for"] + prior) / denominator) / competition_average
    totals["defense_strength"] = ((totals["goals_against"] + prior) / denominator) / competition_average
    totals["competition_avg_goals"] = competition_average
    totals["competition"] = competition
    totals["season"] = TARGET_SEASON

    LOGGER.info("%s: calibrated %d team(s), competition average %.2f goals/match",
                competition, len(totals), competition_average)
    return totals


def save_poisson_stats(connection, stats: pd.DataFrame) -> None:
    """Persist the calibrated rates."""
    if stats.empty:
        return
    columns = ["team_id", "team_name", "competition", "season", "matches_played",
               "avg_goals_scored", "avg_goals_conceded", "attack_strength",
               "defense_strength", "competition_avg_goals"]
    statement = f"""
        INSERT INTO poisson_team_stats ({", ".join(columns)}) VALUES %s
        ON CONFLICT (team_id, competition, season) DO UPDATE SET
            matches_played = EXCLUDED.matches_played,
            avg_goals_scored = EXCLUDED.avg_goals_scored,
            avg_goals_conceded = EXCLUDED.avg_goals_conceded,
            attack_strength = EXCLUDED.attack_strength,
            defense_strength = EXCLUDED.defense_strength,
            competition_avg_goals = EXCLUDED.competition_avg_goals,
            calculated_at = NOW()
    """
    values = [tuple(row) for row in stats[columns].itertuples(index=False, name=None)]
    with connection:
        with connection.cursor() as cursor:
            execute_values(cursor, statement, values, page_size=200)


def poisson_rates(stats: pd.DataFrame) -> dict[int, dict[str, float]]:
    """Index the calibrated strengths by team id for fast lookup."""
    return {
        int(row.team_id): {
            "attack": float(row.attack_strength),
            "defense": float(row.defense_strength),
            "average": float(row.competition_avg_goals),
        }
        for row in stats.itertuples(index=False)
    }


def simulate_legs(
    rng: np.random.Generator, rates: dict[int, dict[str, float]],
    home_ids: np.ndarray, away_ids: np.ndarray, neutral: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample goals for a set of ties, vectorised over simulations.

    Expected goals for a side are its attack strength times the opponent's
    defensive weakness times the competition average -- the standard
    Dixon-Coles style construction without the low-score correction.
    """
    fallback = {"attack": 1.0, "defense": 1.0, "average": 1.4}
    home = np.array([rates.get(int(i), fallback)["attack"] for i in home_ids])
    home_def = np.array([rates.get(int(i), fallback)["defense"] for i in home_ids])
    away = np.array([rates.get(int(i), fallback)["attack"] for i in away_ids])
    away_def = np.array([rates.get(int(i), fallback)["defense"] for i in away_ids])
    average = np.array([rates.get(int(i), fallback)["average"] for i in home_ids])

    advantage = 1.0 if neutral else HOME_ADVANTAGE
    lambda_home = home * away_def * average * advantage
    lambda_away = away * home_def * average
    return rng.poisson(lambda_home), rng.poisson(lambda_away)


# ============================================================
# Fixture probabilities
# ============================================================

def load_model() -> tuple[Any, list[str]]:
    """Load the production model and the exact column order it expects."""
    for name in ("match_outcome_lr_v1", "match_outcome_xgb_v1"):
        model_path = MODELS_DIR / f"{name}.pkl"
        if model_path.is_file():
            metadata = json.loads((MODELS_DIR / f"{name}_metadata.json").read_text(encoding="utf-8"))
            LOGGER.info("Using model %s (%s)", name, metadata.get("algorithm", "unknown"))
            return joblib.load(model_path), metadata["features_used"]
    raise FileNotFoundError("No trained model found. Run scripts/train_model.py first.")


def load_pending_fixtures(connection, competition: str) -> pd.DataFrame:
    """Read every unplayed fixture of the target season, with its features."""
    query = tm.SELECT_FEATURES.replace(
        "WHERE result IS NOT NULL",
        "WHERE result IS NULL AND competition = %s AND season = %s",
    ).replace("SELECT season,", "SELECT match_id, home_team_id, away_team_id, competition_phase AS phase, season,")
    with connection.cursor() as cursor:
        cursor.execute(query, (competition, TARGET_SEASON))
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns)


def fixture_probabilities(model, feature_columns: list[str], fixtures: pd.DataFrame) -> np.ndarray:
    """Score every fixture once, returning an (n_fixtures, 3) probability array.

    Scoring once and reusing the result is what makes ten thousand simulations
    tractable. The probabilities do not depend on the simulation, so calling the
    model inside the loop would repeat identical work millions of times.
    """
    scored = fixtures.copy()
    scored["result"] = "H"  # placeholder; prepare_matrix needs the column present
    features, _, _ = tm.prepare_matrix(scored, feature_columns=feature_columns)
    return model.predict_proba(features)


def sample_outcomes(rng: np.random.Generator, probabilities: np.ndarray, simulations: int) -> np.ndarray:
    """Draw an outcome for every fixture in every simulation at once.

    Inverse-CDF sampling against one uniform per fixture per simulation. Doing
    this as a single array operation rather than a Python loop is the difference
    between seconds and hours.
    """
    cumulative = probabilities.cumsum(axis=1)
    uniforms = rng.random((simulations, len(probabilities)))
    return (uniforms[:, :, None] > cumulative[None, :, :]).sum(axis=2)


def points_from_outcomes(
    outcomes: np.ndarray, home_index: np.ndarray, away_index: np.ndarray, team_count: int,
) -> np.ndarray:
    """Accumulate league points per team across every simulation."""
    simulations = outcomes.shape[0]
    points = np.zeros((simulations, team_count), dtype=np.int32)

    home_points = np.where(outcomes == HOME_WIN, 3, np.where(outcomes == DRAW, 1, 0))
    away_points = np.where(outcomes == AWAY_WIN, 3, np.where(outcomes == DRAW, 1, 0))

    # np.add.at accumulates in place and handles a team appearing many times,
    # which plain fancy indexing would silently collapse to a single write.
    np.add.at(points, (slice(None), home_index), home_points)
    np.add.at(points, (slice(None), away_index), away_points)
    return points


# ============================================================
# La Liga
# ============================================================

def current_table(connection, competition: str) -> pd.DataFrame:
    """Return points and goal difference already banked this season."""
    query = """
        SELECT team_id, team_name,
               COALESCE(SUM(points), 0)                        AS points,
               COALESCE(SUM(goals_for) - SUM(goals_against), 0) AS goal_difference,
               COUNT(*)                                        AS matches_played
        FROM vw_team_match_results
        WHERE competition = %s AND season = %s
        GROUP BY team_id, team_name
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (competition, TARGET_SEASON))
        columns = [description[0] for description in cursor.description]
        return pd.DataFrame(cursor.fetchall(), columns=columns)


def rank_simulations(points: np.ndarray, goal_difference: np.ndarray,
                    rng: np.random.Generator) -> np.ndarray:
    """Rank teams within each simulation, best first.

    Ties break on goal difference already banked this season, then on noise
    drawn independently for every simulation. The noise must be random rather
    than a fixed per-team offset: in a competition where no match has been
    played yet the goal differences are all zero, so a constant offset would
    decide every tie the same way and hand the title to whichever team happened
    to sit earliest in the array. That produced a 36-team Champions League in
    which the third-best side by rating won three times as often as the best.

    Real tiebreakers include head-to-head records, which the simulation does not
    track; against a title probability the difference is immaterial.
    """
    # Points dominate, goal difference separates level teams, and the noise --
    # smaller than one goal -- only ever splits an exact tie.
    noise = rng.random(points.shape) * 1e-3
    score = points.astype(float) * 1000.0 + goal_difference[None, :].astype(float) + noise
    # argsort ascending on the negated score gives descending order.
    return np.argsort(-score, axis=1)


def monte_carlo_league(
    connection, model, feature_columns: list[str], competition: str,
    simulations: int, rng: np.random.Generator,
) -> pd.DataFrame:
    """Play the remaining league fixtures many times and count the outcomes."""
    fixtures = load_pending_fixtures(connection, competition)
    table = current_table(connection, competition)

    if table.empty:
        LOGGER.warning("%s: no played matches this season, cannot seed a table", competition)
        return pd.DataFrame()

    teams = table[["team_id", "team_name"]].drop_duplicates().reset_index(drop=True)
    index_of = {int(team_id): position for position, team_id in enumerate(teams["team_id"])}

    starting_points = table.set_index("team_id")["points"].reindex(teams["team_id"]).fillna(0).to_numpy()
    goal_difference = table.set_index("team_id")["goal_difference"].reindex(teams["team_id"]).fillna(0).to_numpy()

    if fixtures.empty:
        LOGGER.info("%s: season already complete", competition)
        totals = np.tile(starting_points, (simulations, 1))
    else:
        known = fixtures[fixtures["home_team_id"].isin(index_of) & fixtures["away_team_id"].isin(index_of)]
        if len(known) < len(fixtures):
            LOGGER.warning("%s: %d fixture(s) involve teams absent from the table, skipped",
                           competition, len(fixtures) - len(known))
        probabilities = fixture_probabilities(model, feature_columns, known)
        outcomes = sample_outcomes(rng, probabilities, simulations)
        home_index = known["home_team_id"].map(index_of).to_numpy()
        away_index = known["away_team_id"].map(index_of).to_numpy()
        totals = starting_points[None, :] + points_from_outcomes(outcomes, home_index, away_index, len(teams))

    ranking = rank_simulations(totals, goal_difference, rng)
    positions = np.empty_like(ranking)
    np.put_along_axis(positions, ranking, np.arange(len(teams))[None, :].repeat(simulations, axis=0), axis=1)

    teams = teams.copy()
    teams["title_probability"] = (positions == 0).mean(axis=0)
    teams["top4_probability"] = (positions < 4).mean(axis=0)
    teams["cl_qualification"] = (positions < 5).mean(axis=0)
    teams["matches_played"] = int(table["matches_played"].max())
    return teams.sort_values("title_probability", ascending=False).reset_index(drop=True)


# ============================================================
# Champions League
# ============================================================

def simulate_knockout(
    rng: np.random.Generator, rates: dict[int, dict[str, float]], contenders: np.ndarray,
) -> np.ndarray:
    """Play a bracket down to one winner per simulation.

    Every round is two legs decided on aggregate goals, except the final. A tie
    is settled by a coin flip, which is the honest model for a shootout: no
    evidence supports predicting one.
    """
    survivors = contenders
    while survivors.shape[1] > 1:
        rng.permuted(survivors, axis=1, out=survivors)
        half = survivors.shape[1] // 2
        home, away = survivors[:, :half], survivors[:, half:]

        final_round = half == 1
        if final_round:
            home_goals, away_goals = simulate_legs(rng, rates, home.ravel(), away.ravel(), neutral=True)
            home_goals, away_goals = home_goals.reshape(home.shape), away_goals.reshape(away.shape)
        else:
            first_home, first_away = simulate_legs(rng, rates, home.ravel(), away.ravel())
            second_away, second_home = simulate_legs(rng, rates, away.ravel(), home.ravel())
            home_goals = (first_home + second_home).reshape(home.shape)
            away_goals = (first_away + second_away).reshape(away.shape)

        coin = rng.random(home_goals.shape) < 0.5
        home_advances = (home_goals > away_goals) | ((home_goals == away_goals) & coin)
        survivors = np.where(home_advances, home, away)

    return survivors[:, 0]


def monte_carlo_champions(
    connection, model, feature_columns: list[str], simulations: int, rng: np.random.Generator,
) -> pd.DataFrame:
    """Simulate the Swiss league phase and the knockout bracket that follows.

    Under the format introduced in 2024/25 the 36 entrants play eight matches
    each in a single table. Places 1-8 reach the round of 16 directly, 9-24
    contest a play-off for the remaining eight places, and 25-36 are eliminated.
    """
    fixtures = load_pending_fixtures(connection, "CL")
    league_phase = fixtures[fixtures["phase"] == "league"]
    if league_phase.empty:
        LOGGER.warning("CL: no pending league-phase fixtures for %d", TARGET_SEASON)
        return pd.DataFrame()

    team_ids = pd.unique(pd.concat([league_phase["home_team_id"], league_phase["away_team_id"]]))
    teams = pd.DataFrame({"team_id": team_ids})
    index_of = {int(team_id): position for position, team_id in enumerate(team_ids)}

    probabilities = fixture_probabilities(model, feature_columns, league_phase)
    outcomes = sample_outcomes(rng, probabilities, simulations)
    home_index = league_phase["home_team_id"].map(index_of).to_numpy()
    away_index = league_phase["away_team_id"].map(index_of).to_numpy()
    points = points_from_outcomes(outcomes, home_index, away_index, len(team_ids))

    ranking = rank_simulations(points, np.zeros(len(team_ids)), rng)
    ordered_ids = np.asarray(team_ids)[ranking]

    stats = calculate_poisson_stats(connection, "CL")
    rates = poisson_rates(stats)

    # Places 9-24 pair off; the eight winners join the top eight in the round of
    # 16. Pairings are random, since UEFA's seeding is not modelled.
    playoff_entrants = ordered_ids[:, CL_DIRECT_QUALIFIERS:CL_PLAYOFF_PLACES].copy()
    rng.permuted(playoff_entrants, axis=1, out=playoff_entrants)
    half = playoff_entrants.shape[1] // 2
    first, second = playoff_entrants[:, :half], playoff_entrants[:, half:]
    leg_one_home, leg_one_away = simulate_legs(rng, rates, first.ravel(), second.ravel())
    leg_two_away, leg_two_home = simulate_legs(rng, rates, second.ravel(), first.ravel())
    aggregate_first = (leg_one_home + leg_two_home).reshape(first.shape)
    aggregate_second = (leg_one_away + leg_two_away).reshape(second.shape)
    coin = rng.random(aggregate_first.shape) < 0.5
    playoff_winners = np.where(
        (aggregate_first > aggregate_second) | ((aggregate_first == aggregate_second) & coin), first, second
    )

    round_of_16 = np.concatenate([ordered_ids[:, :CL_DIRECT_QUALIFIERS], playoff_winners], axis=1)
    champions = simulate_knockout(rng, rates, round_of_16)

    reached_knockout = (ordered_ids[:, :CL_DIRECT_QUALIFIERS, None] == np.asarray(team_ids)[None, None, :]).any(axis=1)
    in_round_of_16 = (round_of_16[:, :, None] == np.asarray(team_ids)[None, None, :]).any(axis=1)

    teams["title_probability"] = [(champions == team_id).mean() for team_id in team_ids]
    teams["knockout_probability"] = in_round_of_16.mean(axis=0)
    teams["top4_probability"] = reached_knockout.mean(axis=0)
    teams["matches_played"] = 0

    names = _team_names(connection, list(team_ids))
    teams["team_name"] = teams["team_id"].map(names)
    return teams.sort_values("title_probability", ascending=False).reset_index(drop=True)


def _team_names(connection, team_ids: list[int]) -> dict[int, str]:
    """Resolve display names for a set of team ids."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT home_team_id, home_team FROM matches WHERE home_team_id = ANY(%s)",
            ([int(team_id) for team_id in team_ids],),
        )
        return {int(team_id): name for team_id, name in cursor.fetchall()}


# ============================================================
# Persistence
# ============================================================

def save_title_probabilities(connection, teams: pd.DataFrame, competition: str,
                             simulations: int, model_version: str, notes: str) -> None:
    """Store one Monte Carlo run."""
    if teams.empty:
        return
    statement = """
        INSERT INTO title_probabilities (
            team_id, team_name, competition, season, title_probability,
            top4_probability, cl_qualification, knockout_probability,
            simulations_used, model_version, matches_played, notes
        ) VALUES %s
        ON CONFLICT (team_id, competition, season, calculated_at) DO NOTHING
    """
    values = [
        (int(row.team_id), row.team_name, competition, TARGET_SEASON, float(row.title_probability),
         float(getattr(row, "top4_probability", 0.0)), float(getattr(row, "cl_qualification", 0.0)),
         float(getattr(row, "knockout_probability", 0.0)), simulations, model_version,
         int(row.matches_played), notes)
        for row in teams.itertuples(index=False)
    ]
    with connection:
        with connection.cursor() as cursor:
            execute_values(cursor, statement, values, page_size=200)


# ============================================================
# Player projections
# ============================================================

def project_player_stats(connection, competition: str, rng: np.random.Generator,
                         resamples: int = 5_000) -> pd.DataFrame:
    """Project Barcelona's scorers and assisters to the end of the season.

    The interval comes from a bootstrap rather than a formula. A player's rate
    is treated as a Poisson intensity and the remaining fixtures are replayed
    thousands of times; the 15th and 85th percentiles of the totals become the
    interval. That propagates the real source of uncertainty -- a small number
    of matches observed -- instead of assuming a fixed percentage band.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT player_id, player_name, team_id, team, goals, assists
            FROM scorers WHERE competition = %s AND season = %s AND team_id = %s
            """,
            (competition, TARGET_SEASON, BARCELONA_TEAM_ID),
        )
        columns = [description[0] for description in cursor.description]
        players = pd.DataFrame(cursor.fetchall(), columns=columns)

        cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'FINISHED') AS played,
                   COUNT(*) FILTER (WHERE status <> 'FINISHED') AS remaining
            FROM matches
            WHERE competition = %s AND season = %s
              AND (home_team_id = %s OR away_team_id = %s)
            """,
            (competition, TARGET_SEASON, BARCELONA_TEAM_ID, BARCELONA_TEAM_ID),
        )
        played, remaining = cursor.fetchone()

    if players.empty or not played:
        LOGGER.warning("%s: no Barcelona scorers recorded for %d yet", competition, TARGET_SEASON)
        return pd.DataFrame()

    priors = _prior_rates(connection, competition)

    rows = []
    for player in players.itertuples(index=False):
        for metric in ("goals", "assists"):
            current = int(getattr(player, metric) or 0)
            observed_rate = current / played

            # Shrink the observed rate toward a prior instead of extrapolating
            # it. Five goals in three matches is a rate of 1.67 per match, and
            # projecting that across the remaining thirty-five gives sixty-three
            # goals -- a number no league has ever seen. The player is not that
            # good; the sample is that small.
            #
            # A Gamma-Poisson posterior mean does the shrinking: the prior is
            # worth PRIOR_MATCH_WEIGHT matches of evidence, so early in a season
            # it dominates and by mid-season the player's own record takes over.
            prior_rate = priors.get((int(player.player_id), metric), priors.get(("default", metric), 0.3))
            rate = (prior_rate * PRIOR_MATCH_WEIGHT + current) / (PRIOR_MATCH_WEIGHT + played)

            simulated = current + rng.poisson(rate, size=(resamples, max(remaining, 0))).sum(axis=1)
            rows.append({
                "player_id": int(player.player_id),
                "player_name": player.player_name,
                "team_id": int(player.team_id),
                "team_name": player.team,
                "competition": competition,
                "season": TARGET_SEASON,
                "metric": metric,
                "current_value": current,
                "matches_played": int(played),
                "matches_remaining": int(remaining),
                "rate_per_match": rate,
                "projection_central": float(np.median(simulated)),
                "projection_min": float(np.percentile(simulated, 15)),
                "projection_max": float(np.percentile(simulated, 85)),
                "low_confidence": played < 5,
                "notes": "Early season: fewer than five matches observed" if played < 5 else None,
            })
    return pd.DataFrame(rows)


def _prior_rates(connection, competition: str) -> dict[tuple, float]:
    """Return last season's scoring and assist rates, used as priors.

    A player's own record from the previous season is the best available prior.
    Where there is none -- a new signing, or someone outside last season's top
    ten -- the average across that season's scorers stands in, which is a far
    better guess than the player's three-match sample.
    """
    query = """
        SELECT s.player_id, s.goals, s.assists,
               (SELECT COUNT(*) FROM matches m
                 WHERE m.competition = s.competition AND m.season = s.season
                   AND m.status = 'FINISHED'
                   AND (m.home_team_id = s.team_id OR m.away_team_id = s.team_id)) AS team_matches
        FROM scorers s
        WHERE s.competition = %s AND s.season = %s
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (competition, TARGET_SEASON - 1))
        rows = cursor.fetchall()

    priors: dict[tuple, float] = {}
    totals = {"goals": [], "assists": []}
    for player_id, goals, assists, team_matches in rows:
        if not team_matches:
            continue
        for metric, value in (("goals", goals), ("assists", assists)):
            rate = (value or 0) / team_matches
            priors[(int(player_id), metric)] = rate
            totals[metric].append(rate)

    for metric, values in totals.items():
        priors[("default", metric)] = float(np.mean(values)) if values else 0.3

    LOGGER.info("%s: priors from season %d for %d player(s); defaults %.3f goals, %.3f assists",
                competition, TARGET_SEASON - 1, len(rows),
                priors[("default", "goals")], priors[("default", "assists")])
    return priors


def save_player_projections(connection, projections: pd.DataFrame) -> None:
    """Store the player projections."""
    if projections.empty:
        return
    columns = ["player_id", "player_name", "team_id", "team_name", "competition", "season",
               "metric", "current_value", "matches_played", "matches_remaining", "rate_per_match",
               "projection_central", "projection_min", "projection_max", "low_confidence", "notes"]
    statement = f"""
        INSERT INTO player_projections ({", ".join(columns)}) VALUES %s
        ON CONFLICT (player_id, competition, season, metric, calculated_at) DO NOTHING
    """
    values = [tuple(row) for row in projections[columns].itertuples(index=False, name=None)]
    with connection:
        with connection.cursor() as cursor:
            execute_values(cursor, statement, values, page_size=200)


# ============================================================
# Entry point
# ============================================================

def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS)
    parser.add_argument("--seed", type=int, default=42, help="Fixed by default so a run is reproducible")
    parser.add_argument("--no-save", action="store_true", help="Report without writing to the database")
    return parser.parse_args()


def _report(label: str, teams: pd.DataFrame, column: str = "title_probability") -> None:
    """Log the leading contenders of one competition."""
    if teams.empty:
        LOGGER.warning("%s: nothing to report", label)
        return
    LOGGER.info("--- %s ---", label)
    for row in teams.head(5).itertuples(index=False):
        LOGGER.info("  %-28s %5.1f%%", row.team_name, getattr(row, column) * 100)
    total = teams[column].sum()
    LOGGER.info("  probabilities sum to %.4f across %d team(s)", total, len(teams))


def main() -> int:
    """Run the full prediction pipeline."""
    tm.configure_logging()
    args = parse_args()
    rng = np.random.default_rng(args.seed)

    model, feature_columns = load_model()
    connection = get_connection()

    try:
        for competition in ("PD", "CL"):
            stats = calculate_poisson_stats(connection, competition)
            if not args.no_save:
                save_poisson_stats(connection, stats)

        LOGGER.info("Running %d simulations per competition", args.simulations)

        liga = monte_carlo_league(connection, model, feature_columns, "PD", args.simulations, rng)
        _report("La Liga %d/%d title" % (TARGET_SEASON, TARGET_SEASON + 1 - 2000), liga)

        champions = monte_carlo_champions(connection, model, feature_columns, args.simulations, rng)
        _report("Champions League title", champions)

        if not args.no_save:
            save_title_probabilities(connection, liga, "PD", args.simulations, "lr_v1",
                                     "Season in progress")
            save_title_probabilities(connection, champions, "CL", args.simulations, "lr_v1+poisson",
                                     "New league-phase format; no matches played yet")

        for competition in ("PD", "CL"):
            projections = project_player_stats(connection, competition, rng)
            if not projections.empty:
                LOGGER.info("--- %s player projections ---", competition)
                goals = projections[projections["metric"] == "goals"].nlargest(3, "projection_central")
                for row in goals.itertuples(index=False):
                    LOGGER.info("  %-28s %2d now -> %.0f (%.0f-%.0f)", row.player_name, row.current_value,
                                row.projection_central, row.projection_min, row.projection_max)
                if not args.no_save:
                    save_player_projections(connection, projections)

        barca_liga = liga[liga["team_id"] == BARCELONA_TEAM_ID]["title_probability"]
        barca_cl = champions[champions["team_id"] == BARCELONA_TEAM_ID]["title_probability"] if not champions.empty else pd.Series(dtype=float)
        LOGGER.info("=" * 52)
        LOGGER.info("  Barcelona, season %d/%d", TARGET_SEASON, TARGET_SEASON + 1 - 2000)
        LOGGER.info("    La Liga title      %5.1f%%", (barca_liga.iloc[0] if len(barca_liga) else 0) * 100)
        LOGGER.info("    Champions League   %5.1f%%", (barca_cl.iloc[0] if len(barca_cl) else 0) * 100)
        LOGGER.info("  Ratings held fixed across simulations; see module docstring.")
        LOGGER.info("=" * 52)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
