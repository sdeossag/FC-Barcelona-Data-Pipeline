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
-- renaming every column that follows.
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
    ROUND(EXTRACT(EPOCH FROM (ingested_at - produced_at)) * 1000) AS pipeline_latency_ms
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
