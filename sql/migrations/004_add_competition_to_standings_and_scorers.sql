-- ============================================================
-- Migration 004: make competition part of the standings and scorers keys
-- ============================================================
-- Both tables recorded which season a row belonged to but not which
-- competition. That left a latent collision: a player who scores in both La
-- Liga and the Champions League in the same season produced two rows with the
-- identical key (player_id, team_id, season), so the upsert overwrote one with
-- the other and a competition's figures silently disappeared.
--
-- It has not happened yet only because the two competitions currently report
-- different season years. That is a coincidence of the calendar, not a
-- guarantee, and it would break the first time the years line up.
--
-- Unlike migration 001, this one recreates the tables instead of migrating them
-- in place. standings and scorers are snapshots of the current league state,
-- not an accumulating history, and every row is reproducible from the JSON
-- already in data/raw/. Re-running transform and load restores them exactly.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/004_add_competition_to_standings_and_scorers.sql
--   docker exec barca-airflow-scheduler python /opt/airflow/scripts/transform.py \
--     --input-dir /opt/airflow/data/raw --output-dir /opt/airflow/data/staging
--   docker exec barca-airflow-scheduler python /opt/airflow/scripts/load.py \
--     --input-dir /opt/airflow/data/staging

BEGIN;

DO $$
BEGIN
    RAISE NOTICE 'Rebuilding standings (% row(s)) and scorers (% row(s)) from data/raw',
        (SELECT COUNT(*) FROM warehouse.standings),
        (SELECT COUNT(*) FROM warehouse.scorers);
END
$$;

DROP TABLE IF EXISTS warehouse.standings;
DROP TABLE IF EXISTS warehouse.scorers;

CREATE TABLE warehouse.standings (
    competition   TEXT NOT NULL,
    team_id       INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    team_name     TEXT NOT NULL,
    played        INTEGER NOT NULL DEFAULT 0,
    won           INTEGER NOT NULL DEFAULT 0,
    drawn         INTEGER NOT NULL DEFAULT 0,
    lost          INTEGER NOT NULL DEFAULT 0,
    goals_for     INTEGER NOT NULL DEFAULT 0,
    goals_against INTEGER NOT NULL DEFAULT 0,
    goal_diff     INTEGER NOT NULL DEFAULT 0,
    points        INTEGER NOT NULL DEFAULT 0,
    season        INTEGER,
    matchday      INTEGER,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (competition, team_id, season, matchday)
);

CREATE TABLE warehouse.scorers (
    competition TEXT NOT NULL,
    player_id   INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    team_id     INTEGER NOT NULL,
    team        TEXT NOT NULL,
    goals       INTEGER NOT NULL DEFAULT 0,
    assists     INTEGER NOT NULL DEFAULT 0,
    penalties   INTEGER NOT NULL DEFAULT 0,
    season      INTEGER,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (competition, player_id, team_id, season)
);

-- Names left the primary keys, so they no longer have an implicit index.
-- Power BI and ad-hoc SQL still filter on them.
CREATE INDEX standings_team_name_idx ON warehouse.standings (team_name);
CREATE INDEX scorers_player_name_idx ON warehouse.scorers (player_name);

COMMIT;
