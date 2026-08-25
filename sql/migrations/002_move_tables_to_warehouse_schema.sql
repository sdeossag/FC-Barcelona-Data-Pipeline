-- ============================================================
-- Migration 002: move the analytics tables into a warehouse schema
-- ============================================================
-- The tables were created in public, the schema every PostgreSQL database
-- starts with and where anything else in the database also lands by default.
-- Giving the warehouse its own schema separates the analytics tables from
-- everything else, and makes it possible to grant a BI tool read access to the
-- warehouse alone rather than to the whole database.
--
-- ALTER TABLE ... SET SCHEMA only rewrites catalog entries. No rows are copied,
-- and indexes, constraints and the primary key move with the table.
--
-- Run with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/migrations/002_move_tables_to_warehouse_schema.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS warehouse;

-- Move each table only if it is still sitting in public, so re-running this
-- migration after it has already been applied is a no-op instead of an error.
-- The loop variable is deliberately not called table_name: PL/pgSQL would then
-- resolve both sides of the comparison below to the same identifier and reject
-- the query as ambiguous.
DO $$
DECLARE
    target_table TEXT;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['matches', 'standings', 'scorers', 'live_events']
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables t
            WHERE t.table_schema = 'public' AND t.table_name = target_table
        ) THEN
            EXECUTE format('ALTER TABLE public.%I SET SCHEMA warehouse', target_table);
            RAISE NOTICE 'Moved public.% to warehouse', target_table;
        ELSE
            RAISE NOTICE 'Skipped %: not present in public', target_table;
        END IF;
    END LOOP;
END
$$;

COMMIT;

-- The application does not rely on a database-level default: scripts/load.py
-- sets search_path per connection from POSTGRES_SCHEMA. This default only makes
-- interactive psql sessions land in the right place without extra typing.
ALTER DATABASE barca_warehouse SET search_path TO warehouse, public;
