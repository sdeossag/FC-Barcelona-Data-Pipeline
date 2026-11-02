-- ============================================================
-- Migration 005: record the competition stage on each match
-- ============================================================
-- The Champions League restarts matchday numbering at 1 for every knockout
-- round, so a season contains several matches labelled "matchday 1": one from
-- the league phase, one from the round of 16, one from the quarter-finals. Any
-- chart that accumulates points by matchday silently mixes them together.
--
-- football-data.org already returns a stage field (LEAGUE_STAGE, PLAYOFFS,
-- LAST_16, QUARTER_FINALS, SEMI_FINALS, FINAL); it was simply not being stored.
--
-- The matches primary key is match_id and does not change, so unlike migration
-- 001 this needs no key swap: adding a nullable column and re-running the
-- pipeline lets the existing upsert fill it in.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/005_add_stage_to_matches.sql
--   then re-run transform.py and load.py over data/raw

BEGIN;

ALTER TABLE warehouse.matches ADD COLUMN IF NOT EXISTS stage TEXT;

CREATE INDEX IF NOT EXISTS matches_stage_idx ON warehouse.matches (competition, season, stage);

COMMIT;
