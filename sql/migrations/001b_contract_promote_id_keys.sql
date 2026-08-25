-- ============================================================
-- Migration 001b (CONTRACT): promote identifiers to primary keys
-- ============================================================
-- Part two of the expand -> migrate -> contract migration. Apply this only
-- after 001a and after re-running the pipeline so every row has its ids.
--
-- This step is the destructive one: it enforces NOT NULL, swaps the primary
-- keys, and removes any row the backfill could not resolve. Those rows are
-- recoverable, because data/raw/ still holds the JSON they came from.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/001b_contract_promote_id_keys.sql

BEGIN;

-- ------------------------------------------------------------
-- Guard: refuse to continue if the backfill has not run
-- ------------------------------------------------------------
-- Without this, the migration would quietly delete every row when applied out
-- of order. Raising inside the transaction rolls everything back instead.
DO $$
DECLARE
    missing_count INTEGER;
BEGIN
    SELECT
        (SELECT COUNT(*) FROM standings WHERE team_id IS NULL)
      + (SELECT COUNT(*) FROM scorers   WHERE player_id IS NULL OR team_id IS NULL)
      + (SELECT COUNT(*) FROM matches   WHERE home_team_id IS NULL OR away_team_id IS NULL)
    INTO missing_count;

    IF missing_count > 0 THEN
        RAISE EXCEPTION
            'Backfill incomplete: % row(s) still lack identifiers. Re-run the pipeline over data/raw/ before applying 001b.',
            missing_count;
    END IF;
END
$$;

-- ------------------------------------------------------------
-- matches: keep match_id as the key, enforce the new columns
-- ------------------------------------------------------------
ALTER TABLE matches
    ALTER COLUMN home_team_id SET NOT NULL,
    ALTER COLUMN away_team_id SET NOT NULL;

-- ------------------------------------------------------------
-- standings: (team_name, season, matchday) -> (team_id, season, matchday)
-- ------------------------------------------------------------
ALTER TABLE standings
    ALTER COLUMN team_id SET NOT NULL;

ALTER TABLE standings
    DROP CONSTRAINT IF EXISTS standings_pkey;

ALTER TABLE standings
    ADD CONSTRAINT standings_pkey PRIMARY KEY (team_id, season, matchday);

-- ------------------------------------------------------------
-- scorers: (player_name, team, season) -> (player_id, team_id, season)
-- ------------------------------------------------------------
ALTER TABLE scorers
    ALTER COLUMN player_id SET NOT NULL,
    ALTER COLUMN team_id   SET NOT NULL;

ALTER TABLE scorers
    DROP CONSTRAINT IF EXISTS scorers_pkey;

ALTER TABLE scorers
    ADD CONSTRAINT scorers_pkey PRIMARY KEY (player_id, team_id, season);

-- ------------------------------------------------------------
-- Indexes for the columns humans still query by
-- ------------------------------------------------------------
-- The names are no longer part of a key, so they lost their implicit index.
-- Power BI and ad-hoc SQL still filter on them, so index them explicitly.
CREATE INDEX IF NOT EXISTS standings_team_name_idx ON standings (team_name);
CREATE INDEX IF NOT EXISTS scorers_player_name_idx ON scorers (player_name);
CREATE INDEX IF NOT EXISTS matches_home_team_id_idx ON matches (home_team_id);
CREATE INDEX IF NOT EXISTS matches_away_team_id_idx ON matches (away_team_id);

COMMIT;
