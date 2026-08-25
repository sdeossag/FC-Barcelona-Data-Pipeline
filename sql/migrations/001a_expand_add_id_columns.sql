-- ============================================================
-- Migration 001a (EXPAND): add stable identifier columns
-- ============================================================
-- Part one of an expand -> migrate -> contract migration.
--
-- The warehouse originally keyed standings and scorers on team and player
-- names. Names are display data: football-data.org can rename "FC Barcelona"
-- to "Barcelona" at any time, and the upsert would stop recognising the row,
-- insert a duplicate, and silently split the history in two.
--
-- This step is additive and backward compatible. The new columns are nullable
-- so existing rows stay valid and the current pipeline keeps working while the
-- backfill runs. Nothing is dropped here.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/001a_expand_add_id_columns.sql

BEGIN;

-- Matches already had a stable primary key (match_id), so these columns are
-- added only to make joins to standings and scorers possible without matching
-- on free text.
ALTER TABLE matches
    ADD COLUMN IF NOT EXISTS home_team_id INTEGER,
    ADD COLUMN IF NOT EXISTS away_team_id INTEGER;

ALTER TABLE standings
    ADD COLUMN IF NOT EXISTS team_id INTEGER;

ALTER TABLE scorers
    ADD COLUMN IF NOT EXISTS player_id INTEGER,
    ADD COLUMN IF NOT EXISTS team_id INTEGER;

COMMIT;

-- Next step: re-run the pipeline over the existing files in data/raw/ so the
-- upsert populates the new columns on every existing row. Because the old
-- primary keys are still in place, the upsert updates rows instead of
-- duplicating them. Then apply 001b to promote the new keys.
