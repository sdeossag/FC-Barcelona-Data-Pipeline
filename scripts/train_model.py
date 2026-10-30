"""Train and evaluate the match-outcome models.

Two questions are settled here with measurements rather than opinion.

The first is what "good" means. Reporting an accuracy alone is meaningless in a
three-class problem where the classes are not balanced: home advantage is real,
so a model that always predicts a home win already scores in the mid-forties.
That constant predictor is the baseline every model is compared against, and a
model that fails to beat it has learned nothing.

The second is whether the two competitions deserve separate models. The obvious
answer is yes, since they are different tournaments; the obvious answer is also
untested, and the Champions League has only a few hundred training rows. Both
arrangements are trained and compared, and the numbers decide.

A third concern runs underneath both. These probabilities are not the end
product: the Monte Carlo simulation samples from them thousands of times. A
model can be accurate and still badly calibrated, and a simulation built on
overconfident probabilities produces confident nonsense. Log loss and the Brier
score measure that, so they are reported alongside accuracy.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    classification_report,
    confusion_matrix,
    log_loss,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier
import joblib

from db import get_connection


LOGGER = logging.getLogger("football_model_training")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPOSITORY_ROOT / "models"

# Encoded so the classes are ordered by how favourable they are to the home
# side. XGBoost treats them as unordered labels, but the ordering keeps the
# confusion matrix readable.
RESULT_ENCODING = {"A": 0, "D": 1, "H": 2}
RESULT_LABELS = ["Away win", "Draw", "Home win"]

# Evaluation holds out the most recent complete season. The split is by time,
# never at random: a random split would put April's matches in the training set
# and January's in the test set, letting the model learn from a future it will
# not have when it runs for real. Reported accuracy would rise and mean nothing.
TRAIN_THROUGH_SEASON = 2024
TEST_SEASON = 2025

NUMERIC_FEATURES = [
    "home_elo", "away_elo", "elo_difference",
    "home_form_gf", "home_form_ga", "away_form_gf", "away_form_ga",
    "home_form_points", "away_form_points",
    "home_form_matches", "away_form_matches",
    "h2h_home_wins", "h2h_draws", "h2h_away_wins",
    "h2h_home_gf", "h2h_away_gf", "h2h_matches",
]
CATEGORICAL_FEATURES = ["competition_phase", "competition"]

SELECT_FEATURES = """
    SELECT season, competition, competition_phase, is_neutral_venue, result,
           home_elo, away_elo, elo_difference,
           home_form_gf, home_form_ga, away_form_gf, away_form_ga,
           home_form_points, away_form_points, home_form_matches, away_form_matches,
           h2h_home_wins, h2h_draws, h2h_away_wins, h2h_home_gf, h2h_away_gf, h2h_matches
    FROM ml_features
    WHERE result IS NOT NULL
    ORDER BY match_date
"""


def configure_logging() -> None:
    """Configure timestamped logs for command-line and Airflow execution."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def load_training_data(connection) -> pd.DataFrame:
    """Read every played match that has features attached."""
    with connection.cursor() as cursor:
        cursor.execute(SELECT_FEATURES)
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    frame = pd.DataFrame(rows, columns=columns)
    LOGGER.info("Loaded %d labelled match(es) from ml_features", len(frame))
    return frame


def prepare_matrix(frame: pd.DataFrame, feature_columns: list[str] | None = None) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """Turn the raw feature rows into a numeric matrix and an encoded target.

    The categorical columns are one-hot encoded and then reindexed onto a fixed
    column list. Without that reindex, a split whose test set happens to contain
    no final would produce fewer columns than the training set, and the model
    would fail on a shape mismatch at exactly the wrong moment.
    """
    numeric = frame[NUMERIC_FEATURES].astype(float)
    neutral = frame[["is_neutral_venue"]].astype(int)
    dummies = pd.get_dummies(frame[CATEGORICAL_FEATURES], prefix=CATEGORICAL_FEATURES, dtype=int)

    features = pd.concat([numeric, neutral, dummies], axis=1)
    if feature_columns is not None:
        features = features.reindex(columns=feature_columns, fill_value=0)

    target = frame["result"].map(RESULT_ENCODING).astype(int)
    return features, target, list(features.columns)


def temporal_split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split by season, holding out the most recent complete one."""
    train = frame[frame["season"] <= TRAIN_THROUGH_SEASON]
    test = frame[frame["season"] == TEST_SEASON]
    return train, test


def build_baseline() -> DummyClassifier:
    """Return the constant predictor every model must beat.

    Always predicting the most frequent class is not a strawman: home advantage
    makes home wins the plurality outcome in football, so this scores far above
    the 33% a three-class problem might suggest.
    """
    return DummyClassifier(strategy="most_frequent")


def build_logistic_regression() -> Pipeline:
    """Return a scaled logistic regression, the interpretable reference model.

    Scaling matters here and not for the trees: the Elo columns run in the
    thousands while the form columns sit between 0 and 5, and a regression
    penalises coefficients on a shared scale.
    """
    return Pipeline([
        ("scaler", StandardScaler()),
        # multi_class is not passed: it was removed in scikit-learn 1.7, and
        # multinomial is the default for the lbfgs solver regardless.
        ("classifier", LogisticRegression(max_iter=1000)),
    ])


def build_xgboost() -> XGBClassifier:
    """Return the gradient-boosted classifier.

    The parameters are chosen for a few hundred to a few thousand rows, which is
    the regime this project is in:
      n_estimators=200   trees built in sequence, each correcting the last
      max_depth=4        shallow trees; deeper ones memorise a set this size
      learning_rate=0.05 each tree contributes little, so errors accumulate slowly
      subsample=0.8      each tree sees 80% of rows, which decorrelates them
      colsample_bytree=0.8  and 80% of columns, for the same reason
    The last three all trade a little fit for a lot less overfitting.
    """
    return XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="multi:softprob",
        eval_metric="mlogloss",
        random_state=42,
        n_jobs=2,
    )


def evaluate(model, features: pd.DataFrame, target: pd.Series, name: str) -> dict[str, Any]:
    """Score a model and log a readable report.

    Accuracy is reported per class as well as overall. Draws are the hard case
    for every football model: they have no dedicated signal the way a strong
    favourite does, and a model that never predicts one can still look accurate.
    """
    predictions = model.predict(features)
    probabilities = model.predict_proba(features)

    accuracy = accuracy_score(target, predictions)
    logloss = log_loss(target, probabilities, labels=[0, 1, 2])

    # Brier score on the home-win probability: how far the stated confidence
    # sits from what happened. The simulation samples these probabilities, so
    # how well they are calibrated matters as much as how often they are right.
    brier = brier_score_loss((target == RESULT_ENCODING["H"]).astype(int), probabilities[:, RESULT_ENCODING["H"]])

    LOGGER.info("--- %s ---", name)
    LOGGER.info("accuracy %.4f | log loss %.4f | brier (home) %.4f", accuracy, logloss, brier)
    LOGGER.info("\n%s", classification_report(target, predictions, labels=[0, 1, 2],
                                              target_names=RESULT_LABELS, zero_division=0))

    matrix = confusion_matrix(target, predictions, labels=[0, 1, 2])
    header = "actual \\ predicted".ljust(20) + "".join(label.rjust(11) for label in RESULT_LABELS)
    lines = [header]
    for label, row in zip(RESULT_LABELS, matrix):
        lines.append(label.ljust(20) + "".join(str(value).rjust(11) for value in row))
    LOGGER.info("\n%s", "\n".join(lines))

    return {"name": name, "accuracy": float(accuracy), "log_loss": float(logloss), "brier_home": float(brier)}


def feature_importance(model: XGBClassifier, feature_names: list[str]) -> pd.DataFrame:
    """Rank the features by how much the trees relied on them."""
    frame = pd.DataFrame({"feature": feature_names, "importance": model.feature_importances_})
    return frame.sort_values("importance", ascending=False).reset_index(drop=True)


def save_model(model, name: str, metadata: dict[str, Any]) -> Path:
    """Persist a model and the metadata describing how it was produced.

    The metadata is what makes a stored model auditable months later: which
    seasons it saw, how it scored, and on which columns. A .pkl alone answers
    none of that, and it is not in version control either.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODELS_DIR / f"{name}.pkl"
    joblib.dump(model, model_path)
    (MODELS_DIR / f"{name}_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    LOGGER.info("Saved %s", model_path)
    return model_path


def train_and_compare(train: pd.DataFrame, test: pd.DataFrame, scope: str) -> dict[str, Any]:
    """Train the three models on one scope and return their scores."""
    X_train, y_train, columns = prepare_matrix(train)
    X_test, y_test, _ = prepare_matrix(test, feature_columns=columns)

    LOGGER.info("=== %s: %d training rows, %d test rows, %d features ===",
                scope, len(X_train), len(X_test), len(columns))

    results = {}
    models = {}
    for label, builder in (
        ("baseline (always home)", build_baseline),
        ("logistic regression", build_logistic_regression),
        ("xgboost", build_xgboost),
    ):
        model = builder()
        model.fit(X_train, y_train)
        results[label] = evaluate(model, X_test, y_test, f"{scope} | {label}")
        models[label] = model

    return {"scope": scope, "results": results, "models": models, "columns": columns,
            "n_train": len(X_train), "n_test": len(X_test)}


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-save", action="store_true", help="Evaluate without writing any model to disk")
    return parser.parse_args()


def main() -> int:
    """Compare model arrangements, then fit and store the winner."""
    configure_logging()
    args = parse_args()

    connection = get_connection()
    try:
        frame = load_training_data(connection)
    finally:
        connection.close()

    if frame.empty:
        LOGGER.error("ml_features holds no labelled rows. Run feature_engineering.py first.")
        return 1

    train, test = temporal_split(frame)

    # Arrangement 1: one model over both competitions, with competition as a
    # feature. Arrangement 2: a separate model per competition. The Champions
    # League has only a few hundred training rows, so the combined model may
    # well win on it despite being less specialised.
    experiments = [train_and_compare(train, test, "combined")]
    for competition in sorted(frame["competition"].unique()):
        experiments.append(train_and_compare(
            train[train["competition"] == competition],
            test[test["competition"] == competition],
            competition,
        ))

    LOGGER.info("=" * 74)
    LOGGER.info("%-22s %8s %10s %10s %10s", "scope | model", "n_train", "accuracy", "log loss", "vs base")
    LOGGER.info("-" * 74)
    for experiment in experiments:
        base_accuracy = experiment["results"]["baseline (always home)"]["accuracy"]
        for label, scores in experiment["results"].items():
            delta = scores["accuracy"] - base_accuracy
            LOGGER.info("%-22s %8d %10.4f %10.4f %+10.4f",
                        f"{experiment['scope']} | {label[:12]}", experiment["n_train"],
                        scores["accuracy"], scores["log_loss"], delta)
    LOGGER.info("=" * 74)

    combined = experiments[0]
    LOGGER.info("Top features:\n%s",
                feature_importance(combined["models"]["xgboost"], combined["columns"]).head(8).to_string(index=False))

    # The algorithm is chosen by measurement, not decided in advance. Log loss
    # decides rather than accuracy: these probabilities feed a Monte Carlo
    # simulation that samples them thousands of times, so how well the stated
    # confidence matches reality matters more than how often the top pick is
    # right. On this data the gradient-boosted model overfits and the linear one
    # generalises better, which is the opposite of the usual expectation.
    candidates = {label: scores for label, scores in combined["results"].items() if "baseline" not in label}
    winner = min(candidates, key=lambda label: candidates[label]["log_loss"])
    LOGGER.info("Selected for production: %s (log loss %.4f, accuracy %.4f)",
                winner, candidates[winner]["log_loss"], candidates[winner]["accuracy"])

    # A model that cannot beat a constant predictor has learned nothing, and
    # shipping it would be worse than shipping nothing: it would look principled.
    baseline_accuracy = combined["results"]["baseline (always home)"]["accuracy"]
    if candidates[winner]["accuracy"] <= baseline_accuracy:
        LOGGER.error("Selected model does not beat the constant predictor (%.4f). Not saving.", baseline_accuracy)
        return 1

    if args.no_save:
        LOGGER.info("--no-save given; nothing written to disk")
        return 0

    # Refit on every labelled season before storing. Evaluation needed a held
    # out season; the model that goes to production should not be denied it.
    X_all, y_all, columns = prepare_matrix(frame)
    builders = {"logistic regression": build_logistic_regression, "xgboost": build_xgboost}
    production = builders[winner]()
    production.fit(X_all, y_all)

    model_name = "match_outcome_lr_v1" if winner == "logistic regression" else "match_outcome_xgb_v1"
    save_model(production, model_name, {
        "model_name": model_name,
        "algorithm": winner,
        "selected_by": "lowest log loss among non-baseline models on the held-out season",
        "scope": "combined (PD + CL, competition as a feature)",
        "evaluated_train_seasons": f"<= {TRAIN_THROUGH_SEASON}",
        "evaluated_test_season": TEST_SEASON,
        "evaluation": {label: scores for label, scores in combined["results"].items()},
        "fitted_on_rows": int(len(X_all)),
        "fitted_on_seasons": sorted(int(season) for season in frame["season"].unique()),
        "features_used": columns,
        "result_encoding": RESULT_ENCODING,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
