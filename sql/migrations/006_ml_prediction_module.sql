-- ============================================================
-- Migration 006: tables for the ML prediction module
-- ============================================================
-- Adds the storage the prediction module needs: an engineered feature matrix,
-- an Elo history, Poisson rates, Monte Carlo title probabilities and player
-- projections.
--
-- Two deliberate departures from a naive design, both following decisions this
-- project already made:
--
--   * Teams and players are keyed on the numeric id the API assigns, with the
--     name kept alongside for display. Keying on names is what let RCD Espanyol
--     de Barcelona be counted as Barcelona in migration 004's aftermath; the
--     same mistake in a training set would be far harder to notice, because a
--     model does not complain about contaminated labels.
--   * Everything lives in the warehouse schema, like the rest of the tables.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/006_ml_prediction_module.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS warehouse;


-- ------------------------------------------------------------
-- ml_features: one row per match, ready to train on
-- ------------------------------------------------------------
-- Every column is computed from information available BEFORE kick-off. That
-- constraint is the whole point of the table: a feature that peeks at the
-- final score would inflate test accuracy and collapse in production.
--
-- result is NULL for matches that have not been played. Those rows are the
-- prediction set; the rest are the training set.
CREATE TABLE IF NOT EXISTS warehouse.ml_features (
    match_id          BIGINT PRIMARY KEY,
    season            INTEGER NOT NULL,
    competition       TEXT    NOT NULL,
    match_date        TIMESTAMPTZ NOT NULL,

    home_team_id      INTEGER NOT NULL,
    home_team         TEXT    NOT NULL,
    away_team_id      INTEGER NOT NULL,
    away_team         TEXT    NOT NULL,

    -- Elo, computed separately per competition: a side can dominate its league
    -- and struggle in Europe, and one shared rating would blur both.
    home_elo          DOUBLE PRECISION,
    away_elo          DOUBLE PRECISION,
    elo_difference    DOUBLE PRECISION,

    -- Rolling form over the previous N matches, excluding this one.
    home_form_gf      DOUBLE PRECISION,
    home_form_ga      DOUBLE PRECISION,
    away_form_gf      DOUBLE PRECISION,
    away_form_ga      DOUBLE PRECISION,
    home_form_points  DOUBLE PRECISION,
    away_form_points  DOUBLE PRECISION,
    -- How many matches the form window actually covered. Early in a season it
    -- is below the window, and the model should be able to tell.
    home_form_matches INTEGER,
    away_form_matches INTEGER,

    -- Head to head, from the home side's perspective.
    h2h_home_wins     INTEGER DEFAULT 0,
    h2h_draws         INTEGER DEFAULT 0,
    h2h_away_wins     INTEGER DEFAULT 0,
    h2h_home_gf       DOUBLE PRECISION DEFAULT 0,
    h2h_away_gf       DOUBLE PRECISION DEFAULT 0,
    h2h_matches       INTEGER DEFAULT 0,

    -- Normalised phase, derived from matches.stage. The raw values are not
    -- comparable across competitions: La Liga says REGULAR_SEASON where the
    -- Champions League says LEAGUE_STAGE, and the latter only exists from
    -- 2024/25 onward, when the group stage was replaced.
    competition_phase TEXT NOT NULL,
    is_neutral_venue  BOOLEAN NOT NULL DEFAULT FALSE,

    -- Target: H home win, D draw, A away win. NULL until the match is played.
    result            CHAR(1),

    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ml_features_result_valid
        CHECK (result IS NULL OR result IN ('H', 'D', 'A')),
    CONSTRAINT ml_features_phase_valid
        CHECK (competition_phase IN ('league', 'group', 'playoff', 'r16', 'qf', 'sf', 'final'))
);

CREATE INDEX IF NOT EXISTS ml_features_training_idx
    ON warehouse.ml_features (competition, season)
    WHERE result IS NOT NULL;

CREATE INDEX IF NOT EXISTS ml_features_pending_idx
    ON warehouse.ml_features (competition, season, match_date)
    WHERE result IS NULL;


-- ------------------------------------------------------------
-- team_elo_history: the rating after every match
-- ------------------------------------------------------------
-- Storing the rating after each match, rather than only the current one, is
-- what makes the features reproducible: rebuilding the matrix for a past date
-- must use the rating as it stood then, not as it stands today. Keeping only
-- the latest value would leak future information into past rows.
CREATE TABLE IF NOT EXISTS warehouse.team_elo_history (
    team_id        INTEGER NOT NULL,
    team_name      TEXT    NOT NULL,
    competition    TEXT    NOT NULL,
    after_match_id BIGINT  NOT NULL,
    after_date     TIMESTAMPTZ NOT NULL,
    season         INTEGER NOT NULL,
    elo_before     DOUBLE PRECISION NOT NULL,
    elo_after      DOUBLE PRECISION NOT NULL,

    -- A team appears once per match it played, in one competition.
    PRIMARY KEY (team_id, competition, after_match_id)
);

CREATE INDEX IF NOT EXISTS team_elo_history_lookup_idx
    ON warehouse.team_elo_history (competition, team_id, after_date DESC);


-- ------------------------------------------------------------
-- poisson_team_stats: calibrated scoring rates
-- ------------------------------------------------------------
-- Strengths are expressed relative to the competition average, so a value of
-- 1.5 means "scores 50% more than a typical side in this competition". That
-- normalisation is what lets La Liga and Champions League rates be compared,
-- and what makes the simulation independent of how high-scoring the era is.
CREATE TABLE IF NOT EXISTS warehouse.poisson_team_stats (
    team_id            INTEGER NOT NULL,
    team_name          TEXT    NOT NULL,
    competition        TEXT    NOT NULL,
    season             INTEGER NOT NULL,
    matches_played     INTEGER NOT NULL,
    avg_goals_scored   DOUBLE PRECISION NOT NULL,
    avg_goals_conceded DOUBLE PRECISION NOT NULL,
    attack_strength    DOUBLE PRECISION NOT NULL,
    defense_strength   DOUBLE PRECISION NOT NULL,
    -- The competition-wide mean these strengths are relative to. Stored so a
    -- simulation can be reproduced without recomputing it from the matches.
    competition_avg_goals DOUBLE PRECISION NOT NULL,
    calculated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (team_id, competition, season)
);


-- ------------------------------------------------------------
-- title_probabilities: Monte Carlo output
-- ------------------------------------------------------------
-- Rows accumulate rather than being replaced: keeping every weekly run is what
-- lets the report show a probability moving over a season, which is far more
-- informative than a single current number. calculated_at is part of the key
-- for that reason.
CREATE TABLE IF NOT EXISTS warehouse.title_probabilities (
    team_id           INTEGER NOT NULL,
    team_name         TEXT    NOT NULL,
    competition       TEXT    NOT NULL,
    season            INTEGER NOT NULL,
    calculated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    title_probability DOUBLE PRECISION NOT NULL,
    top4_probability  DOUBLE PRECISION,   -- La Liga only
    cl_qualification  DOUBLE PRECISION,   -- La Liga only
    knockout_probability DOUBLE PRECISION, -- Champions League only

    simulations_used  INTEGER NOT NULL DEFAULT 10000,
    model_version     TEXT    NOT NULL,
    -- Matches already played when this ran. A probability from matchday 2 and
    -- one from matchday 30 are not comparable, and the reader needs to know.
    matches_played    INTEGER,
    notes             TEXT,

    PRIMARY KEY (team_id, competition, season, calculated_at),

    CONSTRAINT title_probability_is_a_probability
        CHECK (title_probability BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS title_probabilities_latest_idx
    ON warehouse.title_probabilities (competition, season, calculated_at DESC);


-- ------------------------------------------------------------
-- player_projections: goals and assists to end of season
-- ------------------------------------------------------------
-- Central estimate plus an interval, never a bare number. A projection of
-- "24 goals" reads as precision the data cannot support in September; "18 to
-- 31" states the uncertainty the reader is entitled to.
CREATE TABLE IF NOT EXISTS warehouse.player_projections (
    player_id          INTEGER NOT NULL,
    player_name        TEXT    NOT NULL,
    team_id            INTEGER NOT NULL,
    team_name          TEXT    NOT NULL,
    competition        TEXT    NOT NULL,
    season             INTEGER NOT NULL,
    metric             TEXT    NOT NULL,
    calculated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    current_value      INTEGER NOT NULL,
    matches_played     INTEGER NOT NULL,
    matches_remaining  INTEGER NOT NULL,
    rate_per_match     DOUBLE PRECISION NOT NULL,

    projection_central DOUBLE PRECISION NOT NULL,
    projection_min     DOUBLE PRECISION NOT NULL,  -- 15th percentile
    projection_max     DOUBLE PRECISION NOT NULL,  -- 85th percentile

    -- Set when the sample is too small to project honestly, so the report can
    -- mark the row instead of presenting it with the same weight as the rest.
    low_confidence     BOOLEAN NOT NULL DEFAULT FALSE,
    notes              TEXT,

    PRIMARY KEY (player_id, competition, season, metric, calculated_at),

    CONSTRAINT player_projections_metric_valid
        CHECK (metric IN ('goals', 'assists')),
    CONSTRAINT player_projections_interval_ordered
        CHECK (projection_min <= projection_central AND projection_central <= projection_max)
);

CREATE INDEX IF NOT EXISTS player_projections_latest_idx
    ON warehouse.player_projections (competition, season, metric, calculated_at DESC);

COMMIT;
