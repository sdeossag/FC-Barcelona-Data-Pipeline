-- Create a dedicated database for Airflow metadata.
CREATE DATABASE airflow;

-- Create the analytics schema in the warehouse database. This file runs only on
-- the very first PostgreSQL initialization, so a fresh clone comes up with the
-- schema already in place instead of relying on the first pipeline run.
\connect barca_warehouse

CREATE SCHEMA IF NOT EXISTS warehouse;

-- Interactive psql sessions land in the warehouse schema without qualifying
-- every table. The pipeline does not depend on this: scripts/load.py sets
-- search_path per connection from POSTGRES_SCHEMA.
ALTER DATABASE barca_warehouse SET search_path TO warehouse, public;
