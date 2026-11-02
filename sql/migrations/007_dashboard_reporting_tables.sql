-- ============================================================
-- Migration 007: reporting tables for the dashboard
-- ============================================================
-- The dashboard needs three facts that the pipeline computed but never stored:
--
--   * match_predictions  -- the per-fixture probabilities behind a Monte Carlo
--     run. predict.py scores every pending fixture once and reuses the result;
--     keeping only the season-level title probability throws that detail away,
--     so the report cannot show what the model expects from the next match.
--   * model_registry     -- the evaluation that justifies the model. The
--     metadata JSON next to the .pkl is the source of truth, but Power BI
--     cannot read a file next to a model; this table mirrors it.
--   * quality_check_results -- the post-load checks in quality.py already
--     answer "is the warehouse coherent?". Persisting one row per check turns
--     a log line into a report page and gives the checks a history.
--
-- All three are append-only. A probability is a statement about a moment:
-- replacing yesterday's number with today's would hide the movement the report
-- exists to show. The views in sql/views/bi_views.sql select the latest row
-- where a current value is needed.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/007_dashboard_reporting_tables.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS warehouse;


-- ------------------------------------------------------------
-- match_predictions: one row per fixture per prediction run
-- ------------------------------------------------------------
-- predicted_at is part of the key on purpose. Two runs on different days are
-- two observations, and the report can show how the forecast moved. The
-- probability columns are constrained to [0, 1] and to sum to one, so a
-- malformed model output fails at write time instead of rendering as a
-- plausible-looking bar.
CREATE TABLE IF NOT EXISTS warehouse.match_predictions (
    match_id          BIGINT      NOT NULL,
    competition       TEXT        NOT NULL,
    season            INTEGER     NOT NULL,
    match_date        TIMESTAMPTZ NOT NULL,

    home_team_id      INTEGER     NOT NULL,
    home_team         TEXT        NOT NULL,
    away_team_id      INTEGER     NOT NULL,
    away_team         TEXT        NOT NULL,

    prob_home         DOUBLE PRECISION NOT NULL,
    prob_draw         DOUBLE PRECISION NOT NULL,
    prob_away         DOUBLE PRECISION NOT NULL,

    -- Derived once here rather than in DAX: every consumer should agree on
    -- which outcome the model favours and how confident it is.
    predicted_outcome CHAR(1)     NOT NULL,
    confidence        DOUBLE PRECISION NOT NULL,

    model_version     TEXT        NOT NULL,
    predicted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (match_id, model_version, predicted_at),

    CONSTRAINT match_predictions_probabilities_valid
        CHECK (prob_home BETWEEN 0 AND 1
           AND prob_draw BETWEEN 0 AND 1
           AND prob_away BETWEEN 0 AND 1),
    CONSTRAINT match_predictions_probabilities_sum_to_one
        CHECK (ABS(prob_home + prob_draw + prob_away - 1) < 1e-6),
    CONSTRAINT match_predictions_outcome_valid
        CHECK (predicted_outcome IN ('H', 'D', 'A')),
    CONSTRAINT match_predictions_confidence_matches_outcome
        CHECK (confidence = GREATEST(prob_home, prob_draw, prob_away))
);

CREATE INDEX IF NOT EXISTS match_predictions_fixture_idx
    ON warehouse.match_predictions (competition, season, match_date);

CREATE INDEX IF NOT EXISTS match_predictions_latest_idx
    ON warehouse.match_predictions (match_id, predicted_at DESC);


-- ------------------------------------------------------------
-- model_registry: the evaluation behind the production model
-- ------------------------------------------------------------
-- One row per trained model, not per run. The accuracy stored here is the
-- held-out-season result, which is the number a reader should compare against
-- the baseline; it is not the (optimistic) training score.
CREATE TABLE IF NOT EXISTS warehouse.model_registry (
    model_name         TEXT        NOT NULL,
    version            TEXT        NOT NULL,
    algorithm          TEXT        NOT NULL,
    scope              TEXT,
    trained_at         TIMESTAMPTZ,
    evaluated_season   INTEGER,

    accuracy           DOUBLE PRECISION,
    baseline_accuracy  DOUBLE PRECISION,
    log_loss           DOUBLE PRECISION,
    baseline_log_loss  DOUBLE PRECISION,
    brier_home         DOUBLE PRECISION,
    features_used      INTEGER,

    -- The full metadata document is kept alongside the typed columns so a
    -- future report can read a metric that did not exist when this table was
    -- designed, without another migration.
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    notes              TEXT,
    registered_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (model_name, version)
);


-- ------------------------------------------------------------
-- quality_check_results: one row per check per run
-- ------------------------------------------------------------
-- run_id groups the checks executed together. It is generated by quality.py,
-- not by the database, so a caller can retry a run and keep the same identity
-- in its own logs.
CREATE TABLE IF NOT EXISTS warehouse.quality_check_results (
    run_id          UUID        NOT NULL,
    check_name      TEXT        NOT NULL,
    passed          BOOLEAN     NOT NULL,
    observed_value  BIGINT,
    expectation     TEXT,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (run_id, check_name)
);

CREATE INDEX IF NOT EXISTS quality_check_results_latest_idx
    ON warehouse.quality_check_results (checked_at DESC);

CREATE INDEX IF NOT EXISTS quality_check_results_check_idx
    ON warehouse.quality_check_results (check_name, checked_at DESC);

COMMIT;
