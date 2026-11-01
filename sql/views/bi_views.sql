-- ============================================================
-- Reporting views consumed by Power BI
-- ============================================================
-- Power BI connects to these views, never to the base tables. Keeping the
-- reporting logic in SQL rather than in DAX has three practical consequences:
--
--   * It is reviewable. A .pbix file is a binary blob, so measures written
--     inside it never appear in a diff and cannot be reviewed in a pull
--     request. Everything here is plain text under version control.
--   * It survives the tool. The same views feed a notebook, a REST API or a
--     different BI product without rewriting the logic.
--   * It hides physical detail. live_events stores event-specific attributes as
--     JSONB, which Power BI would otherwise import as opaque text.
--
-- Every statement is CREATE OR REPLACE, so this file can be applied repeatedly.
--
-- Apply with:
--   docker exec -i barca-postgres psql -U <user> -d barca_warehouse \
--     < sql/views/bi_views.sql

-- ------------------------------------------------------------
-- Conventions used throughout
-- ------------------------------------------------------------
-- comp_season_key   Every view carries it. Power BI relationships join on a
--                   single column, so competition and season are combined into
--                   one key that vw_dim_competition can filter through. Without
--                   it a slicer only filters the table it was dragged from.
--
-- is_group_stage    The stage column is not comparable across competitions:
--                   La Liga calls it REGULAR_SEASON, the Champions League calls
--                   it LEAGUE_STAGE. This boolean means one filter works for
--                   both instead of the reader having to know each name.
--
-- team_id = 81      FC Barcelona. Matching on the name is wrong: "Barcelona"
--                   also appears in "RCD Espanyol de Barcelona", which silently
--                   folded a second club's results into every Barca figure.


-- ------------------------------------------------------------
-- DIMENSION - the table every slicer should filter through
-- ------------------------------------------------------------
-- A star schema needs one dimension holding the values a user filters by, with
-- one row per combination. Point the competition and season slicers at this
-- view and its relationships propagate the filter to every other view at once.
CREATE OR REPLACE VIEW warehouse.vw_dim_competition AS
SELECT DISTINCT
    competition || '-' || season AS comp_season_key,
    competition,
    season,
    CASE competition
        WHEN 'PD' THEN 'La Liga'
        WHEN 'CL' THEN 'Champions League'
        ELSE competition
    END AS competition_name,
    -- 2025 is displayed as "2025/26", which is how a season is actually named.
    season || '/' || RIGHT((season + 1)::text, 2) AS season_label
FROM warehouse.matches
WHERE season IS NOT NULL;


-- ------------------------------------------------------------
-- One row per team per match
-- ------------------------------------------------------------
-- The matches table stores a fixture from the fixture's point of view: one row
-- holding both sides. Almost every football question is asked from a team's
-- point of view instead, so each match is unfolded into two rows. This is the
-- base most other views build on.
CREATE OR REPLACE VIEW warehouse.vw_team_match_results AS
SELECT
    match_id,
    competition || '-' || season AS comp_season_key,
    competition, season, stage, matchday, date,
    stage IN ('REGULAR_SEASON', 'LEAGUE_STAGE') AS is_group_stage,
    home_team_id AS team_id,
    home_team     AS team_name,
    away_team_id  AS opponent_id,
    away_team     AS opponent_name,
    TRUE          AS is_home,
    home_goals    AS goals_for,
    away_goals    AS goals_against,
    CASE
        WHEN home_goals > away_goals THEN 'WIN'
        WHEN home_goals = away_goals THEN 'DRAW'
        ELSE 'LOSS'
    END AS result,
    CASE
        WHEN home_goals > away_goals THEN 3
        WHEN home_goals = away_goals THEN 1
        ELSE 0
    END AS points
FROM warehouse.matches
WHERE status = 'FINISHED'

UNION ALL

SELECT
    match_id,
    competition || '-' || season,
    competition, season, stage, matchday, date,
    stage IN ('REGULAR_SEASON', 'LEAGUE_STAGE'),
    away_team_id, away_team, home_team_id, home_team,
    FALSE, away_goals, home_goals,
    CASE
        WHEN away_goals > home_goals THEN 'WIN'
        WHEN away_goals = home_goals THEN 'DRAW'
        ELSE 'LOSS'
    END,
    CASE
        WHEN away_goals > home_goals THEN 3
        WHEN away_goals = home_goals THEN 1
        ELSE 0
    END
FROM warehouse.matches
WHERE status = 'FINISHED';


-- ------------------------------------------------------------
-- PAGE 1 - Barcelona's own season figures
-- ------------------------------------------------------------
-- The KPI row is about Barcelona, so it is built from Barcelona's matches
-- alone. League-wide totals live in vw_competition_context below and belong in
-- the visuals that exist to provide context, not in the headline numbers.
CREATE OR REPLACE VIEW warehouse.vw_barca_kpis AS
SELECT
    comp_season_key,
    competition,
    season,
    COUNT(*)                                   AS matches_played,
    SUM(goals_for)                             AS goals_for,
    SUM(goals_against)                         AS goals_against,
    SUM(goals_for) - SUM(goals_against)        AS goal_difference,
    COUNT(*) FILTER (WHERE result = 'WIN')     AS wins,
    COUNT(*) FILTER (WHERE result = 'DRAW')    AS draws,
    COUNT(*) FILTER (WHERE result = 'LOSS')    AS losses,
    SUM(points)                                AS points,
    COUNT(*) FILTER (WHERE is_home)            AS home_matches,
    COUNT(*) FILTER (WHERE NOT is_home)        AS away_matches
FROM warehouse.vw_team_match_results
WHERE team_id = 81
GROUP BY comp_season_key, competition, season;


-- ------------------------------------------------------------
-- PAGE 1 - League-wide context
-- ------------------------------------------------------------
-- Deliberately separate from vw_barca_kpis. Mixing "goals in the competition"
-- and "goals Barcelona scored" in one view is what made the KPI cards ambiguous
-- in the first place: the column name alone did not say whose number it was.
CREATE OR REPLACE VIEW warehouse.vw_competition_context AS
SELECT
    competition || '-' || season AS comp_season_key,
    competition,
    season,
    COUNT(*)                               AS competition_matches,
    SUM(home_goals + away_goals)           AS competition_goals,
    ROUND(AVG(home_goals + away_goals), 2) AS competition_goals_per_match
FROM warehouse.matches
WHERE status = 'FINISHED'
GROUP BY competition, season;


-- ------------------------------------------------------------
-- PAGE 1 - Cumulative points by matchday
-- ------------------------------------------------------------
-- Derived from matches rather than standings on purpose. standings holds a
-- snapshot of the table as it was each time the pipeline ran, so it only builds
-- up history going forward. matches already contains every finished fixture, so
-- the full curve can be computed today.
--
-- Partitioned by stage as well as competition: the Champions League restarts
-- matchday numbering in every knockout round, so accumulating across stages
-- would add a quarter-final onto a league-phase total. Filter the visual on
-- is_group_stage, which is true for both REGULAR_SEASON and LEAGUE_STAGE.
CREATE OR REPLACE VIEW warehouse.vw_points_by_matchday AS
SELECT
    comp_season_key, competition, season, stage, is_group_stage,
    team_id, team_name, matchday,
    team_id = 81    AS is_barca,
    SUM(points)     AS matchday_points,
    SUM(goals_for)  AS goals_for,
    -- A running total over the matchdays already played by this team.
    SUM(SUM(points)) OVER (
        PARTITION BY comp_season_key, stage, team_id
        ORDER BY matchday
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS cumulative_points
FROM warehouse.vw_team_match_results
GROUP BY comp_season_key, competition, season, stage, is_group_stage,
         team_id, team_name, matchday;


-- ------------------------------------------------------------
-- PAGE 1 - Current league table
-- ------------------------------------------------------------
-- standings accumulates one snapshot per pipeline run. This exposes only the
-- most recent snapshot of each competition and season, which is what a "current
-- table" visual should show.
CREATE OR REPLACE VIEW warehouse.vw_standings_latest AS
SELECT
    s.competition || '-' || s.season AS comp_season_key,
    s.team_id = 81 AS is_barca,
    s.*
FROM warehouse.standings s
JOIN (
    SELECT competition, season, MAX(matchday) AS latest_matchday
    FROM warehouse.standings
    GROUP BY competition, season
) latest
  ON  s.competition = latest.competition
  AND s.season      = latest.season
  AND s.matchday    = latest.latest_matchday;


-- ------------------------------------------------------------
-- PAGE 1/2 - Barcelona's goals by matchday
-- ------------------------------------------------------------
-- Scoped to Barcelona and split by venue, which is the comparison worth making:
-- whether the team scores more at home than away. A league-wide total of every
-- goal scored on a matchday says nothing about Barcelona.
CREATE OR REPLACE VIEW warehouse.vw_barca_goals_by_matchday AS
SELECT
    comp_season_key, competition, season, stage, is_group_stage, matchday,
    SUM(goals_for) FILTER (WHERE is_home)     AS home_goals,
    SUM(goals_for) FILTER (WHERE NOT is_home) AS away_goals,
    SUM(goals_for)                            AS goals_for,
    SUM(goals_against)                        AS goals_against
FROM warehouse.vw_team_match_results
WHERE team_id = 81
GROUP BY comp_season_key, competition, season, stage, is_group_stage, matchday;


-- ------------------------------------------------------------
-- PAGE 2 - Goals by matchday across the whole competition
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW warehouse.vw_goals_by_matchday AS
SELECT
    competition || '-' || season AS comp_season_key,
    competition,
    season,
    stage,
    stage IN ('REGULAR_SEASON', 'LEAGUE_STAGE') AS is_group_stage,
    matchday,
    SUM(home_goals)              AS home_goals,
    SUM(away_goals)              AS away_goals,
    SUM(home_goals + away_goals) AS total_goals,
    COUNT(*)                     AS matches_played
FROM warehouse.matches
WHERE status = 'FINISHED'
GROUP BY competition, season, stage, matchday;


-- ------------------------------------------------------------
-- PAGE 2 - Shot quality: expected goals against real outcomes
-- ------------------------------------------------------------
-- This replaces the possession-versus-goals idea. Possession is not available:
-- football-data.org sells it as a paid statistics add-on and the free tier
-- never returns it. Expected goals comes from the StatsBomb events already
-- flowing through the streaming layer, so it is real data the project owns.
-- Dropped first because CREATE OR REPLACE VIEW can only append columns at the
-- end of the list: it cannot insert one, and reordering reads to PostgreSQL as
-- renaming every column that follows. The dependent aggregate is dropped too,
-- so this file can be re-applied to an existing warehouse.
DROP VIEW IF EXISTS warehouse.vw_player_shot_quality;
DROP VIEW IF EXISTS warehouse.vw_shot_quality;

CREATE VIEW warehouse.vw_shot_quality AS
SELECT
    -- Provenance, carried explicitly. This view has no comp_season_key and is
    -- deliberately left out of the star schema: it describes StatsBomb events
    -- from a season the batch layer does not cover, and the two sources do not
    -- even share an id space (football-data.org ids run in the hundreds of
    -- thousands, StatsBomb's in the millions). Joining them is not possible,
    -- and a season slicer must not appear to filter it.
    'StatsBomb 2020/21' AS data_source,
    match_id,
    team_name,
    player_name,
    minute,
    (detail->>'expected_goals')::numeric AS expected_goals,
    detail->>'body_part'                 AS body_part,
    detail->>'play_pattern'              AS play_pattern,

    -- Two shapes of the same fact, because they do different jobs. The boolean
    -- is for filtering and for splitting a legend; the integer is what a chart
    -- axis can actually sum. Power BI refuses to aggregate a boolean, so a
    -- scatter plotting goals against expected goals needs the numeric one.
    event_type = 'GOAL'                  AS scored,
    CASE WHEN event_type = 'GOAL' THEN 1 ELSE 0 END AS goals_scored,

    -- Positive means the shot beat its expected value: a goal from a chance
    -- worth 0.05 xG contributes +0.95. Summed per player it reads directly as
    -- over- or under-performance.
    CASE WHEN event_type = 'GOAL' THEN 1 ELSE 0 END - (detail->>'expected_goals')::numeric
        AS xg_difference
FROM warehouse.live_events
WHERE event_type IN ('GOAL', 'SHOT')
  AND detail ? 'expected_goals';


-- ------------------------------------------------------------
-- PAGE 3 - Top scorers, comparable across competitions
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW warehouse.vw_top_scorers AS
SELECT
    competition || '-' || season AS comp_season_key,
    competition,
    season,
    player_id,
    player_name,
    team_id,
    team,
    team_id = 81      AS is_barca,
    goals,
    assists,
    penalties,
    goals - penalties AS open_play_goals,
    -- Ranked inside each competition so a visual can show both leagues side by
    -- side without one drowning out the other.
    RANK() OVER (PARTITION BY competition, season ORDER BY goals DESC) AS scoring_rank
FROM warehouse.scorers;


-- ------------------------------------------------------------
-- PAGE 4 - Live feed
-- ------------------------------------------------------------
-- The JSONB detail column is unfolded into typed columns here, because Power BI
-- would otherwise import it as a single opaque text field.
CREATE OR REPLACE VIEW warehouse.vw_live_events AS
SELECT
    event_id,
    match_id,
    period,
    minute,
    second,
    event_type,
    player_name,
    team_name,
    (detail->>'expected_goals')::numeric AS expected_goals,
    detail->>'outcome'                   AS shot_outcome,
    detail->>'body_part'                 AS body_part,
    detail->>'replacement_name'          AS replacement_name,

    -- Event time: when it happened on the pitch.
    event_ts,
    -- Processing time: when this row was written.
    ingested_at,
    -- End-to-end pipeline latency, in milliseconds.
    ROUND(EXTRACT(EPOCH FROM (ingested_at - produced_at)) * 1000) AS pipeline_latency_ms,

    -- Event names as a reader should see them. The stream carries the provider's
    -- machine codes; the report should not.
    CASE event_type
        WHEN 'GOAL'          THEN 'Gol'
        WHEN 'SHOT'          THEN 'Tiro'
        WHEN 'YELLOW_CARD'   THEN 'Tarjeta amarilla'
        WHEN 'RED_CARD'      THEN 'Tarjeta roja'
        WHEN 'PERIOD_END'    THEN 'Fin de periodo'
        WHEN 'PERIOD_START'  THEN 'Inicio de periodo'
        WHEN 'SUBSTITUTION'  THEN 'Cambio'
        WHEN 'PASS'          THEN 'Pase'
        WHEN 'FOUL'          THEN 'Falta'
        WHEN 'OFFSIDE'       THEN 'Fuera de juego'
        WHEN 'CORNER'        THEN 'Córner'
        ELSE initcap(replace(event_type, '_', ' '))
    END AS event_label
FROM warehouse.live_events;


-- ------------------------------------------------------------
-- PAGE 4 - Running score of the match being replayed
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW warehouse.vw_live_score AS
SELECT
    match_id,
    team_name,
    COUNT(*) FILTER (WHERE event_type = 'GOAL')         AS goals,
    COUNT(*) FILTER (WHERE event_type = 'SHOT')         AS shots,
    COUNT(*) FILTER (WHERE event_type = 'YELLOW_CARD')  AS yellow_cards,
    COUNT(*) FILTER (WHERE event_type = 'RED_CARD')     AS red_cards,
    MAX(minute)                                         AS last_minute
FROM warehouse.live_events
WHERE team_name IS NOT NULL
GROUP BY match_id, team_name;


-- ------------------------------------------------------------
-- DEPRECATED - compatibility shim for vw_season_kpis
-- ------------------------------------------------------------
-- This view was split into vw_barca_kpis (Barcelona alone) and
-- vw_competition_context (the whole competition), because holding both scopes
-- in one view under names that did not say whose number they were is what let
-- a defect go unnoticed: Barcelona was selected with a name match that also
-- caught RCD Espanyol de Barcelona.
--
-- It is kept, with the same column names and the corrected logic, so an
-- existing Power BI model keeps refreshing instead of breaking on a missing
-- object. Dropping it outright is the mistake this project's own migrations
-- are written to avoid: expand, migrate, then contract.
--
-- Do not build new visuals on it. Once every report reads the two views above,
-- delete it.
CREATE OR REPLACE VIEW warehouse.vw_season_kpis AS
SELECT
    context.comp_season_key,
    context.competition,
    context.season,
    context.competition_matches         AS matches_played,
    context.competition_goals           AS total_goals,
    context.competition_goals_per_match AS goals_per_match,
    -- Now sourced from vw_barca_kpis, which selects on team_id rather than on
    -- a name pattern. COALESCE covers a competition Barcelona did not play in.
    COALESCE(barca.wins, 0)             AS barca_wins,
    COALESCE(barca.matches_played, 0)   AS barca_matches
FROM warehouse.vw_competition_context context
LEFT JOIN warehouse.vw_barca_kpis barca
       ON barca.comp_season_key = context.comp_season_key;


-- ------------------------------------------------------------
-- PAGE 2 - One row per team per competition and season
-- ------------------------------------------------------------
-- Aggregates the batch layer to season grain so a scatter can plot every team
-- at once: goals scored against goals conceded places attack on one axis and
-- defence on the other, and the champion lands in the corner where both are
-- good. This replaces the expected-goals scatter on that page, which drew on
-- StatsBomb data from a season the rest of the report does not cover.
CREATE OR REPLACE VIEW warehouse.vw_team_season_summary AS
SELECT
    comp_season_key,
    competition,
    season,
    team_id,
    team_name,
    team_id = 81                                       AS is_barca,
    COUNT(*)                                           AS matches_played,
    SUM(goals_for)                                     AS goals_for,
    SUM(goals_against)                                 AS goals_against,
    SUM(goals_for) - SUM(goals_against)                AS goal_difference,
    ROUND(AVG(goals_for), 2)                           AS goals_for_per_match,
    ROUND(AVG(goals_against), 2)                       AS goals_against_per_match,
    COUNT(*) FILTER (WHERE result = 'WIN')             AS wins,
    COUNT(*) FILTER (WHERE result = 'DRAW')            AS draws,
    COUNT(*) FILTER (WHERE result = 'LOSS')            AS losses,
    SUM(points)                                        AS points,
    -- Clean sheets and blanks: the two ends of a defensive record.
    COUNT(*) FILTER (WHERE goals_against = 0)          AS clean_sheets,
    COUNT(*) FILTER (WHERE goals_for = 0)              AS scoreless_matches
FROM warehouse.vw_team_match_results
WHERE is_group_stage
GROUP BY comp_season_key, competition, season, team_id, team_name;

-- ============================================================
-- DASHBOARD REDESIGN - reporting layer for the new pages
-- ============================================================
-- Everything below is additive. The views the original report uses are left
-- untouched, so a stale .pbix keeps refreshing while the new pages are built.
-- Apply this file after migration 007.


-- ------------------------------------------------------------
-- Date dimension
-- ------------------------------------------------------------
-- Power BI needs a contiguous table to mark as a date table and to drive
-- time intelligence. A view is preferable to a calculated table here: the
-- grain is explicit and reviewable, and every report that points at the
-- warehouse gets the same calendar.
CREATE OR REPLACE VIEW warehouse.vw_dim_date AS
SELECT
    d::date AS date,
    EXTRACT(YEAR FROM d)::int    AS year,
    EXTRACT(MONTH FROM d)::int   AS month,
    CASE EXTRACT(MONTH FROM d)::int
        WHEN 1 THEN 'Enero'    WHEN 2 THEN 'Febrero'  WHEN 3 THEN 'Marzo'
        WHEN 4 THEN 'Abril'    WHEN 5 THEN 'Mayo'     WHEN 6 THEN 'Junio'
        WHEN 7 THEN 'Julio'    WHEN 8 THEN 'Agosto'   WHEN 9 THEN 'Septiembre'
        WHEN 10 THEN 'Octubre' WHEN 11 THEN 'Noviembre' WHEN 12 THEN 'Diciembre'
    END AS month_name,
    EXTRACT(QUARTER FROM d)::int AS quarter,
    TO_CHAR(d, 'YYYY-MM')        AS year_month,
    EXTRACT(ISOYEAR FROM d)::int AS iso_year,
    EXTRACT(WEEK FROM d)::int    AS iso_week,
    EXTRACT(DOW FROM d)::int     AS day_of_week,
    CASE EXTRACT(DOW FROM d)::int
        WHEN 0 THEN 'Domingo' WHEN 1 THEN 'Lunes'   WHEN 2 THEN 'Martes'
        WHEN 3 THEN 'Miércoles' WHEN 4 THEN 'Jueves' WHEN 5 THEN 'Viernes'
        WHEN 6 THEN 'Sábado'
    END AS day_name,
    EXTRACT(DOW FROM d)::int IN (0, 6) AS is_weekend
FROM generate_series('2022-01-01'::date, '2027-12-31'::date, '1 day'::interval) AS d;


-- ------------------------------------------------------------
-- Barcelona fixtures, from Barcelona's point of view
-- ------------------------------------------------------------
-- vw_team_match_results already unfolds the fixture into two rows. This view
-- narrows it to Barcelona and adds the display columns a table visual needs:
-- a scoreline as text, a one-letter result and a venue label.
-- Dropped first because the date column changes from timestamp with time zone to
-- date. A view cannot change a column type through CREATE OR REPLACE, and the
-- dependent views below expanded this one's column list when they were created.
DROP VIEW IF EXISTS warehouse.vw_barca_home_away;
DROP VIEW IF EXISTS warehouse.vw_barca_form_last5;
DROP VIEW IF EXISTS warehouse.vw_barca_matches;

CREATE OR REPLACE VIEW warehouse.vw_barca_matches AS
SELECT
    match_id,
    comp_season_key,
    competition,
    season,
    stage,
    is_group_stage,
    matchday,
    -- Cast here, not in the report. Kick-off times arrive in UTC and Power BI
    -- converts a timestamp to the viewer's time zone, so a fixture stored at
    -- 00:00 UTC would render as the previous day. A fixture date is a calendar
    -- day, not an instant, so the conversion belongs in the view.
    date::date AS date,
    opponent_id,
    opponent_name,
    is_home,
    CASE WHEN is_home THEN 'Casa' ELSE 'Fuera' END AS venue,
    goals_for,
    goals_against,
    goals_for - goals_against AS goal_difference,
    goals_for || ' - ' || goals_against AS scoreline,
    result,
    CASE result WHEN 'WIN' THEN 'V' WHEN 'DRAW' THEN 'E' ELSE 'D' END AS result_code,
    points
FROM warehouse.vw_team_match_results
WHERE team_id = 81;


-- ------------------------------------------------------------
-- Last five matches, per competition and season
-- ------------------------------------------------------------
-- The row number is kept in the view so the report can sort by recency
-- without hard-coding a date. Partitioning by comp_season_key means the
-- "last five" is correct after a competition or season slicer is applied.
CREATE OR REPLACE VIEW warehouse.vw_barca_form_last5 AS
SELECT *
FROM (
    SELECT
        m.*,
        ROW_NUMBER() OVER (
            PARTITION BY comp_season_key
            ORDER BY date DESC, matchday DESC
        ) AS recency
    FROM warehouse.vw_barca_matches m
) ranked
WHERE recency <= 5;


-- ------------------------------------------------------------
-- Home vs away split
-- ------------------------------------------------------------
-- Two rows per competition and season: one for each venue. Rates are stored
-- as decimals (0.88, not 88) so Power BI can format them as percentages
-- without a second calculation.
CREATE OR REPLACE VIEW warehouse.vw_barca_home_away AS
SELECT
    comp_season_key,
    competition,
    season,
    CASE WHEN is_home THEN 'Casa' ELSE 'Fuera' END AS venue,
    COUNT(*)                                    AS matches_played,
    COUNT(*) FILTER (WHERE result = 'WIN')       AS wins,
    COUNT(*) FILTER (WHERE result = 'DRAW')      AS draws,
    COUNT(*) FILTER (WHERE result = 'LOSS')      AS losses,
    SUM(goals_for)                               AS goals_for,
    SUM(goals_against)                           AS goals_against,
    ROUND(AVG(goals_for), 2)                     AS goals_for_per_match,
    ROUND(AVG(goals_against), 2)                 AS goals_against_per_match,
    ROUND(
        COUNT(*) FILTER (WHERE result = 'WIN')::numeric / NULLIF(COUNT(*), 0),
        3
    ) AS win_rate,
    SUM(points)                                  AS points
FROM warehouse.vw_barca_matches
GROUP BY comp_season_key, competition, season, is_home;


-- ------------------------------------------------------------
-- Shot quality per player
-- ------------------------------------------------------------
-- vw_shot_quality keeps one row per shot. This aggregates to the player grain
-- the scatter and the efficiency table need. xG is summed, not averaged: a
-- player with twenty shots at 0.1 xG each has generated two expected goals.
CREATE OR REPLACE VIEW warehouse.vw_player_shot_quality AS
SELECT
    player_name,
    team_name,
    COUNT(*)                       AS shots,
    COUNT(*) FILTER (WHERE scored) AS goals,
    SUM(expected_goals)            AS expected_goals,
    SUM(goals_scored)              AS goals_scored,
    SUM(xg_difference)             AS xg_difference,
    ROUND(SUM(goals_scored)::numeric / NULLIF(SUM(expected_goals), 0), 3) AS conversion_ratio,
    ROUND(SUM(expected_goals)::numeric / NULLIF(COUNT(*), 0), 3)         AS xg_per_shot
FROM warehouse.vw_shot_quality
WHERE player_name IS NOT NULL
GROUP BY player_name, team_name;


-- ------------------------------------------------------------
-- Latest fixture predictions
-- ------------------------------------------------------------
-- DISTINCT ON keeps the most recent prediction for each match while leaving
-- the history in the table for a movement chart later.
-- Dropped first: the fixture date changes from timestamp to date, and the
-- dependent fixtures view cannot survive a column type change.
DROP VIEW IF EXISTS warehouse.vw_barca_fixtures;
DROP VIEW IF EXISTS warehouse.vw_latest_match_predictions;

CREATE OR REPLACE VIEW warehouse.vw_latest_match_predictions AS
SELECT DISTINCT ON (match_id)
    match_id,
    competition,
    season,
    match_date::date AS match_date,
    home_team_id,
    home_team,
    away_team_id,
    away_team,
    prob_home,
    prob_draw,
    prob_away,
    predicted_outcome,
    CASE predicted_outcome
        WHEN 'H' THEN home_team
        WHEN 'A' THEN away_team
        ELSE 'Empate'
    END AS predicted_label,
    confidence,
    model_version,
    predicted_at
FROM warehouse.match_predictions
ORDER BY match_id, predicted_at DESC;


-- ------------------------------------------------------------
-- Barcelona's next fixtures
-- ------------------------------------------------------------
-- Only unplayed fixtures are exposed. Once a match finishes it belongs in
-- the results table, not in "next matches"; leaving it here would make the
-- model look clairvoyant after the fact.
CREATE OR REPLACE VIEW warehouse.vw_barca_fixtures AS
SELECT
    p.*,
    p.competition || '-' || p.season AS comp_season_key,
    p.home_team_id = 81 AS is_barca_home,
    CASE WHEN p.home_team_id = 81 THEN p.away_team ELSE p.home_team END AS opponent,
    CASE WHEN p.home_team_id = 81 THEN 'Casa' ELSE 'Fuera' END AS venue,
    p.match_date::date - CURRENT_DATE AS days_until
FROM warehouse.vw_latest_match_predictions p
JOIN warehouse.matches m ON m.match_id = p.match_id
WHERE 81 IN (p.home_team_id, p.away_team_id)
  AND m.status <> 'FINISHED'
  AND p.match_date >= CURRENT_DATE;


-- ------------------------------------------------------------
-- Model evaluation
-- ------------------------------------------------------------
-- The latest registered row per model, with the comparison to the baseline
-- already calculated. A reader should not have to subtract two percentages
-- in their head to see whether the model is worth using.
CREATE OR REPLACE VIEW warehouse.vw_model_metrics AS
SELECT DISTINCT ON (model_name)
    model_name,
    version,
    algorithm,
    scope,
    trained_at,
    evaluated_season,
    accuracy,
    baseline_accuracy,
    accuracy - baseline_accuracy AS accuracy_lift,
    log_loss,
    baseline_log_loss,
    brier_home,
    features_used,
    metadata,
    notes,
    registered_at,
    (accuracy - baseline_accuracy) > 0 AS beats_baseline
FROM warehouse.model_registry
ORDER BY model_name, registered_at DESC;


-- ------------------------------------------------------------
-- Quality checks: latest run and its summary
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW warehouse.vw_quality_results_latest AS
WITH latest AS (
    SELECT run_id
    FROM warehouse.quality_check_results
    ORDER BY checked_at DESC
    LIMIT 1
)
SELECT
    q.run_id,
    q.check_name,
    q.passed,
    q.observed_value,
    q.expectation,
    q.checked_at,
    -- Readable labels live in the view, not in the report. The quality job
    -- writes machine names and English rule text; translating them once keeps
    -- every visual, tooltip and export in the same language.
    CASE q.check_name
        WHEN 'matches_present'                     THEN 'Partidos cargados'
        WHEN 'standings_present'                   THEN 'Clasificación cargada'
        WHEN 'scorers_present'                     THEN 'Goleadores cargados'
        WHEN 'matches_freshness'                   THEN 'Frescura de partidos'
        WHEN 'matches_unique_ids'                  THEN 'IDs de partido únicos'
        WHEN 'standings_games_add_up'              THEN 'Partidos jugados = V + E + D'
        WHEN 'standings_goal_difference_consistent' THEN 'Diferencia de goles coherente'
        WHEN 'matches_no_negative_goals'           THEN 'Goles no negativos'
        WHEN 'matches_finished_not_in_future'      THEN 'Sin finalizados en el futuro'
        WHEN 'matches_status_recognised'           THEN 'Estados reconocidos'
        WHEN 'scorers_penalties_within_goals'      THEN 'Penaltis dentro de los goles'
        WHEN 'standings_played_within_season'      THEN 'Partidos dentro de la temporada'
        ELSE initcap(replace(q.check_name, '_', ' '))
    END AS check_label,
    CASE q.check_name
        WHEN 'matches_present'                     THEN 'La tabla de partidos debe tener al menos una fila'
        WHEN 'standings_present'                   THEN 'La clasificación debe tener al menos una fila'
        WHEN 'scorers_present'                     THEN 'La tabla de goleadores debe tener al menos una fila'
        WHEN 'matches_freshness'                   THEN 'Los partidos deben haberse cargado en las últimas 30 horas'
        WHEN 'matches_unique_ids'                  THEN 'match_id debe ser único en la tabla de partidos'
        WHEN 'standings_games_add_up'              THEN 'Jugados debe ser igual a ganados + empatados + perdidos'
        WHEN 'standings_goal_difference_consistent' THEN 'La diferencia de goles debe ser goles a favor menos goles en contra'
        WHEN 'matches_no_negative_goals'           THEN 'Los goles nunca pueden ser negativos'
        WHEN 'matches_finished_not_in_future'      THEN 'Un partido finalizado no puede tener fecha futura'
        WHEN 'matches_status_recognised'           THEN 'Cada estado debe ser uno de los que documenta la API'
        WHEN 'scorers_penalties_within_goals'      THEN 'Un goleador no puede tener más penaltis que goles'
        ELSE q.expectation
    END AS expectation_es,
    CASE WHEN q.passed THEN 'Superado' ELSE 'Atención' END AS status_label
FROM warehouse.quality_check_results q
JOIN latest USING (run_id);

CREATE OR REPLACE VIEW warehouse.vw_quality_summary AS
SELECT
    run_id,
    MIN(checked_at) AS checked_at,
    COUNT(*)        AS checks_total,
    COUNT(*) FILTER (WHERE passed)     AS checks_passed,
    COUNT(*) FILTER (WHERE NOT passed) AS checks_failed,
    ROUND(
        COUNT(*) FILTER (WHERE passed)::numeric / NULLIF(COUNT(*), 0),
        3
    ) AS pass_rate
FROM warehouse.quality_check_results
GROUP BY run_id;


-- ------------------------------------------------------------
-- Pipeline health
-- ------------------------------------------------------------
-- One row per persisted table, with the exact count and the age of its newest
-- write. Counting every table is cheap at this scale and avoids the estimates
-- in pg_stat_user_tables, which can lag behind the last load.
CREATE OR REPLACE VIEW warehouse.vw_pipeline_health AS
WITH health AS (
    SELECT 'matches' AS table_name, 'Airflow - batch' AS source,
           COUNT(*)::bigint AS row_count, MAX(loaded_at) AS last_write, 30 AS threshold_hours
    FROM warehouse.matches
    UNION ALL
    SELECT 'standings', 'Airflow - batch', COUNT(*)::bigint, MAX(loaded_at), 30
    FROM warehouse.standings
    UNION ALL
    SELECT 'scorers', 'Airflow - batch', COUNT(*)::bigint, MAX(loaded_at), 30
    FROM warehouse.scorers
    UNION ALL
    SELECT 'live_events', 'Kafka - streaming', COUNT(*)::bigint, MAX(ingested_at), 30
    FROM warehouse.live_events
    UNION ALL
    SELECT 'ml_features', 'Prediction - batch', COUNT(*)::bigint, MAX(computed_at), 30
    FROM warehouse.ml_features
    UNION ALL
    SELECT 'title_probabilities', 'Prediction - weekly', COUNT(*)::bigint, MAX(calculated_at), 24 * 8
    FROM warehouse.title_probabilities
    UNION ALL
    SELECT 'player_projections', 'Prediction - weekly', COUNT(*)::bigint, MAX(calculated_at), 24 * 8
    FROM warehouse.player_projections
    UNION ALL
    SELECT 'match_predictions', 'Prediction - weekly', COUNT(*)::bigint, MAX(predicted_at), 24 * 8
    FROM warehouse.match_predictions
    UNION ALL
    SELECT 'quality_check_results', 'Data quality', COUNT(*)::bigint, MAX(checked_at), 30
    FROM warehouse.quality_check_results
)
SELECT
    h.*,
    CASE WHEN h.is_fresh THEN 'Fresca' ELSE 'Vencida' END AS freshness_label
FROM (
    SELECT
        table_name,
        source,
        row_count,
        last_write,
        ROUND(EXTRACT(EPOCH FROM (NOW() - last_write)) / 3600.0, 1) AS age_hours,
        CASE
            WHEN last_write IS NULL THEN FALSE
            ELSE EXTRACT(EPOCH FROM (NOW() - last_write)) / 3600.0 <= threshold_hours
        END AS is_fresh,
        threshold_hours
    FROM health
) h;


-- ------------------------------------------------------------
-- Title race
-- ------------------------------------------------------------
-- Cumulative points for the six best sides of the selected competition and
-- season. The rank comes from the season summary, not from the current
-- standings snapshot, so the set of "direct rivals" stays stable across
-- matchdays and a line does not appear halfway through the chart.
CREATE OR REPLACE VIEW warehouse.vw_title_race AS
WITH rivals AS (
    SELECT
        comp_season_key,
        team_id,
        RANK() OVER (
            PARTITION BY comp_season_key
            ORDER BY points DESC, goal_difference DESC, goals_for DESC
        ) AS rival_rank
    FROM warehouse.vw_team_season_summary
)
SELECT
    p.comp_season_key,
    p.competition,
    p.season,
    p.stage,
    p.is_group_stage,
    p.team_id,
    p.team_name,
    p.matchday,
    p.matchday_points,
    p.goals_for,
    p.cumulative_points,
    p.team_id = 81 AS is_barca,
    COALESCE(r.rival_rank, 99) AS rival_rank,
    COALESCE(r.rival_rank, 99) <= 6 AS is_direct_rival
FROM warehouse.vw_points_by_matchday p
LEFT JOIN rivals r
       ON r.comp_season_key = p.comp_season_key
      AND r.team_id = p.team_id;


-- ------------------------------------------------------------
-- Title probability over time
-- ------------------------------------------------------------
-- One point per team per day, even if the weekly DAG ran twice. Keeping the
-- last run of the day avoids a zig-zag caused by two simulations on the same
-- date, which is noise rather than movement.
CREATE OR REPLACE VIEW warehouse.vw_title_probability_history AS
WITH daily AS (
    SELECT DISTINCT ON (competition, season, team_id, calculated_at::date)
        competition,
        season,
        team_id,
        team_name,
        calculated_at::date AS calculated_date,
        title_probability,
        simulations_used,
        model_version,
        matches_played
    FROM warehouse.title_probabilities
    ORDER BY competition, season, team_id, calculated_at::date, calculated_at DESC
)
SELECT
    d.*,
    d.team_id = 81 AS is_barca,
    RANK() OVER (
        PARTITION BY competition, season, calculated_date
        ORDER BY title_probability DESC
    ) AS probability_rank,
    -- Appended last: CREATE OR REPLACE VIEW can only add columns at the end.
    d.competition || '-' || d.season AS comp_season_key
FROM daily d;


-- ------------------------------------------------------------
-- Live match selection
-- ------------------------------------------------------------
-- The live page shows one match: the one whose events arrived most recently.
-- Selecting it in SQL keeps the report from needing a "latest" measure on
-- every visual and makes the page work with an empty or multi-match table.
CREATE OR REPLACE VIEW warehouse.vw_live_current_match AS
SELECT
    match_id,
    MIN(event_ts)    AS first_event,
    MAX(event_ts)    AS last_event,
    MAX(ingested_at) AS last_ingested,
    COUNT(*)         AS events,
    MAX(minute)      AS last_minute
FROM warehouse.live_events
GROUP BY match_id
ORDER BY MAX(ingested_at) DESC
LIMIT 1;

CREATE OR REPLACE VIEW warehouse.vw_live_events_current AS
SELECT e.*
FROM warehouse.vw_live_events e
JOIN warehouse.vw_live_current_match c USING (match_id);

CREATE OR REPLACE VIEW warehouse.vw_live_current_score AS
SELECT s.*, s.team_name = 'Barcelona' AS is_barca
FROM warehouse.vw_live_score s
JOIN warehouse.vw_live_current_match c USING (match_id);
