-- ============================================================
-- Migration 008: replace the 2020/21 StatsBomb events with current ESPN events
-- ============================================================
-- The streaming layer used to replay a 2020/21 StatsBomb match. That season is
-- five years old and says nothing about the current team, so the producer now
-- publishes real events from this season's Barcelona matches (see
-- streaming/espn.py).
--
-- ESPN events carry detail->>'source' = 'ESPN'. The StatsBomb rows never had a
-- source key, which is what identifies them here.
--
-- DESTRUCTIVE: the StatsBomb rows are deleted. They are reproducible from the
-- open data set, and nothing else in the warehouse depends on them.
--
-- The table keeps its shape: event_id is a UUID derived from the ESPN play id,
-- so the consumer stays idempotent with no schema change.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/008_replace_statsbomb_with_espn_events.sql

BEGIN;

DELETE FROM warehouse.live_events WHERE NOT (detail ? 'source');

COMMIT;
