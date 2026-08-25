-- ============================================================
-- Migration 003: rebuild live_events around real StatsBomb data
-- ============================================================
-- The original table was shaped for randomly generated events: a serial primary
-- key, one flat player and team name, and no notion of when the event actually
-- happened. Real event data needs a different shape.
--
-- What changes and why:
--   * event_id UUID PRIMARY KEY  -- StatsBomb assigns each event a stable id.
--     Keying on it makes the consumer idempotent, so replaying a match or
--     restarting from offset zero cannot duplicate rows.
--   * player_id / team_id       -- identifiers rather than names alone, for the
--     same reason the batch tables were migrated in 001.
--   * detail JSONB              -- event-specific attributes (expected goals on
--     a shot, the incoming player on a substitution) without one wide table of
--     mostly-null columns.
--   * event_ts / produced_at / ingested_at -- event time and processing time
--     kept apart. Their difference is the pipeline's end-to-end latency.
--
-- DESTRUCTIVE: the existing rows are dropped. They were produced by
-- random.choices() in the previous simulator and carry no information; a replay
-- repopulates the table with real events in about ninety seconds.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/003_rebuild_live_events_for_real_data.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS warehouse;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'warehouse' AND table_name = 'live_events'
    ) THEN
        RAISE NOTICE 'Dropping previous live_events table (% simulated row(s))',
            (SELECT COUNT(*) FROM warehouse.live_events);
    END IF;
END
$$;

DROP TABLE IF EXISTS warehouse.live_events;

CREATE TABLE warehouse.live_events (
    -- StatsBomb's own event identifier: the basis for idempotent consumption.
    event_id     UUID PRIMARY KEY,

    match_id     BIGINT NOT NULL,
    period       SMALLINT,
    minute       INTEGER NOT NULL,
    second       SMALLINT NOT NULL,
    event_type   TEXT NOT NULL,

    player_id    INTEGER,
    player_name  TEXT,
    team_id      INTEGER,
    team_name    TEXT,

    -- Attributes that only apply to some event types.
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Event time: when it happened on the pitch.
    event_ts     TIMESTAMPTZ,

    -- Processing time: when the producer published it, and when this row was
    -- written. Keeping all three apart is what makes late or replayed data
    -- analysable instead of indistinguishable from fresh data.
    produced_at  TIMESTAMPTZ,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The dashboard reads a single match in chronological order.
CREATE INDEX live_events_match_idx ON warehouse.live_events (match_id, minute);

-- Filtering to goals and cards is the most common ad-hoc query.
CREATE INDEX live_events_type_idx ON warehouse.live_events (event_type);

COMMIT;
