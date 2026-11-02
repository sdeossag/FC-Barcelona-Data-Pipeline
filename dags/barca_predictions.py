"""Weekly Airflow orchestration for the prediction module.

Why weekly and not daily. A title probability is a statement about a whole
season, and it moves when results move it. Between Monday and Tuesday no
football is played, so a daily run would write six identical rows a week and
bury the real changes in noise. Weekly lines up with the football calendar: by
Monday every weekend fixture has been played and loaded.

Why the models are not retrained here. This DAG predicts; training is a separate,
deliberate act. Refitting weekly on a training set that grew by nine matches
would change the coefficients slightly every week for no reason, and the stored
probabilities would drift for reasons unrelated to football. Retrain when the
season ends, when a competition changes format -- as the Champions League did in
2024/25 -- or when the evaluation in train_model.py shows the model has stopped
beating its baseline.

The DAG depends on barca_etl_pipeline having loaded recent results. It checks
rather than assumes: predicting from a stale warehouse produces numbers that look
current and are not.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from airflow import DAG
from airflow.exceptions import AirflowSkipException
from airflow.operators.python import PythonOperator


LOGGER = logging.getLogger("barca_ml_predictions")
AIRFLOW_ROOT = Path("/opt/airflow")
SCRIPTS_DIR = AIRFLOW_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# How stale the warehouse may be before predicting from it is misleading. Two
# weeks covers an international break, during which no club football is played
# and the data legitimately does not move.
MAX_DATA_AGE_DAYS = 14

SIMULATIONS = 10_000
RANDOM_SEED = 42


def check_etl_ran(**context: Any) -> dict[str, Any]:
    """Confirm the warehouse holds recent results before anything reads it.

    Raising AirflowSkipException marks the run skipped rather than failed.
    That distinction matters operationally: a failure means something is broken
    and should page someone, while a skip means the preconditions were not met
    and there was correctly nothing to do. Downstream tasks are skipped too, so
    no stale probabilities are written.
    """
    from db import get_connection

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT MAX(loaded_at), COUNT(*) FILTER (WHERE status = 'FINISHED')
                FROM matches
                """
            )
            loaded_at, finished = cursor.fetchone()
    finally:
        connection.close()

    if loaded_at is None or not finished:
        raise AirflowSkipException("The warehouse holds no finished matches; run barca_etl_pipeline first.")

    age = datetime.now(timezone.utc) - loaded_at
    if age > timedelta(days=MAX_DATA_AGE_DAYS):
        raise AirflowSkipException(
            f"Warehouse last loaded {age.days} days ago, beyond the {MAX_DATA_AGE_DAYS} day limit. "
            "Predicting from it would produce numbers that look current but are not."
        )

    LOGGER.info("Warehouse is %d day(s) old with %d finished match(es)", age.days, finished)
    return {"data_age_days": age.days, "finished_matches": int(finished)}


def build_features(**context: Any) -> dict[str, int]:
    """Rebuild the feature matrix for every competition.

    The whole history is recomputed rather than only recent matches. Elo is a
    running total: each rating depends on the one before it, so recomputing a
    window would need the state at the window's start and would silently drift
    if it were ever wrong. The full pass takes seconds on this data, which is far
    cheaper than the class of bug the shortcut invites.
    """
    from feature_engineering import build_features as build, load_matches, save_features
    from db import get_connection

    connection = get_connection()
    counts: dict[str, int] = {}
    try:
        for competition in ("PD", "CL"):
            matches = load_matches(connection, competition)
            if matches.empty:
                LOGGER.warning("%s: no matches to process", competition)
                continue
            features, elo_history = build(matches, competition)
            save_features(connection, features, elo_history)
            counts[competition] = int(features["result"].notna().sum())
            LOGGER.info("%s: %d training row(s) refreshed", competition, counts[competition])
    finally:
        connection.close()

    if not counts:
        raise ValueError("No features were built for any competition")
    return counts


def _run_simulation(competition: str) -> dict[str, float]:
    """Simulate one competition and store the resulting probabilities."""
    import predict
    from db import get_connection

    rng = np.random.default_rng(RANDOM_SEED)
    model, feature_columns = predict.load_model()
    connection = get_connection()

    try:
        stats = predict.calculate_poisson_stats(connection, competition)
        predict.save_poisson_stats(connection, stats)

        if competition == "CL":
            teams = predict.monte_carlo_champions(connection, model, feature_columns, SIMULATIONS, rng)
            notes = "League-phase format introduced in 2024/25"
            version = "lr_v1+poisson"
        else:
            teams = predict.monte_carlo_league(connection, model, feature_columns, competition, SIMULATIONS, rng)
            notes = "Season in progress"
            version = "lr_v1"

        if teams.empty:
            raise ValueError(f"{competition}: simulation produced no results")

        predict.save_title_probabilities(connection, teams, competition, SIMULATIONS, version, notes)

        # The per-fixture probabilities behind the simulation were previously
        # discarded. Persisting them lets the report show what the model expects
        # from the next match, not only the season-level outcome.
        model_name, metadata = predict.active_model()
        model_version = predict.register_model(connection, model_name, metadata)
        stored = predict.save_fixture_predictions(
            connection, model, feature_columns, competition, model_version
        )

        barca = teams[teams["team_id"] == predict.BARCELONA_TEAM_ID]["title_probability"]
        probability = float(barca.iloc[0]) if len(barca) else 0.0
        LOGGER.info("%s: Barcelona title probability %.1f%%, %d fixture prediction(s) stored",
                    competition, probability * 100, stored)
        return {
            "competition": competition,
            "barca_title_probability": probability,
            "fixture_predictions": stored,
        }
    finally:
        connection.close()


def monte_carlo_liga(**context: Any) -> dict[str, float]:
    """Simulate the remaining La Liga season."""
    return _run_simulation("PD")


def monte_carlo_champions(**context: Any) -> dict[str, float]:
    """Simulate the Champions League league phase and knockout rounds."""
    return _run_simulation("CL")


def project_players(**context: Any) -> dict[str, int]:
    """Project Barcelona's scorers and assisters for both competitions."""
    import predict
    from db import get_connection

    rng = np.random.default_rng(RANDOM_SEED)
    connection = get_connection()
    counts: dict[str, int] = {}
    try:
        for competition in ("PD", "CL"):
            projections = predict.project_player_stats(connection, competition, rng)
            if projections.empty:
                LOGGER.info("%s: no projections available yet", competition)
                counts[competition] = 0
                continue
            predict.save_player_projections(connection, projections)
            counts[competition] = len(projections)
    finally:
        connection.close()
    return counts


def log_summary(**context: Any) -> None:
    """Report the week's figures in one place.

    Both simulations are read from XCom rather than recomputed, so the summary
    describes exactly the run that was stored.
    """
    task_instance = context["ti"]
    liga = task_instance.xcom_pull(task_ids="monte_carlo_liga") or {}
    champions = task_instance.xcom_pull(task_ids="monte_carlo_champions") or {}
    players = task_instance.xcom_pull(task_ids="project_players") or {}
    freshness = task_instance.xcom_pull(task_ids="check_etl_ran") or {}

    LOGGER.info("=" * 56)
    LOGGER.info("  Barcelona title probabilities")
    LOGGER.info("    La Liga           %5.1f%%", liga.get("barca_title_probability", 0.0) * 100)
    LOGGER.info("    Champions League  %5.1f%%", champions.get("barca_title_probability", 0.0) * 100)
    LOGGER.info("  Player projections stored: %s", players)
    LOGGER.info("  Warehouse age at run time: %s day(s)", freshness.get("data_age_days", "unknown"))
    LOGGER.info("=" * 56)


def alert_on_failure(context: dict[str, Any]) -> None:
    """Emit a structured alert once a task has exhausted its retries."""
    task_instance = context.get("task_instance")
    LOGGER.error(
        "PREDICTION FAILURE | task=%s run=%s | %s",
        task_instance.task_id if task_instance else "unknown",
        context.get("run_id"),
        context.get("exception"),
    )


default_args = {
    "owner": "samuel",
    # One retry only. These tasks read a database and run a simulation; unlike
    # the extraction DAG they do not depend on a third-party API, so a transient
    # failure is far less likely and a long retry chain would just delay the
    # report.
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=30),
    "on_failure_callback": alert_on_failure,
}

with DAG(
    dag_id="barca_ml_predictions",
    default_args=default_args,
    description="Weekly Monte Carlo title probabilities and player projections",
    schedule="@weekly",
    start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=1),
    tags=["barca", "ml", "predictions"],
) as dag:
    check = PythonOperator(task_id="check_etl_ran", python_callable=check_etl_ran)
    features = PythonOperator(task_id="build_features", python_callable=build_features)
    liga = PythonOperator(task_id="monte_carlo_liga", python_callable=monte_carlo_liga)
    champions = PythonOperator(task_id="monte_carlo_champions", python_callable=monte_carlo_champions)
    players = PythonOperator(task_id="project_players", python_callable=project_players)
    summary = PythonOperator(task_id="log_summary", python_callable=log_summary)

    # The two simulations share no state and read the same features, so they run
    # side by side. Everything after them waits for both.
    check >> features >> [liga, champions] >> players >> summary
