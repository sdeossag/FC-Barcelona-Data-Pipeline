"""Transform football-data.org raw JSON files into clean Parquet datasets."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd


LOGGER = logging.getLogger("football_data_transformer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "data" / "staging"

MATCH_COLUMNS = [
    "match_id", "date", "home_team", "away_team", "home_goals", "away_goals",
    "status", "competition", "matchday", "season",
]
STANDING_COLUMNS = [
    "position", "team_name", "played", "won", "drawn", "lost", "goals_for",
    "goals_against", "goal_diff", "points", "season", "matchday",
]
SCORER_COLUMNS = ["player_name", "team", "goals", "assists", "penalties", "season"]


def configure_logging() -> None:
    """Configure timestamped logs for command-line and Airflow execution."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def _read_json(json_path: str | Path) -> dict[str, Any]:
    """Read one raw API response and validate that its root is an object."""
    path = Path(json_path)
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _season_from(payload: dict[str, Any]) -> int | None:
    """Extract the season start year from the API payload."""
    season = payload.get("season")
    if isinstance(season, dict):
        season = season.get("startDate", "")[:4]
    if season is None:
        season = payload.get("filters", {}).get("season")
    try:
        return int(season) if season not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _output_path(json_path: str | Path, output_dir: str | Path) -> Path:
    """Build a matching Parquet filename in the staging directory."""
    return Path(output_dir) / f"{Path(json_path).stem}.parquet"


def _save_and_log(frame: pd.DataFrame, json_path: str | Path, output_dir: str | Path, kind: str, input_count: int) -> pd.DataFrame:
    """Save one transformed frame and report input/output row counts."""
    output_path = _output_path(json_path, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(output_path, index=False)
    LOGGER.info("%s: %d input rows -> %d output rows; saved to %s", kind, input_count, len(frame), output_path)
    return frame


def transform_matches(json_path: str | Path, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> pd.DataFrame:
    """Transform match records and save the result as Parquet."""
    payload = _read_json(json_path)
    raw_rows = payload.get("matches") or []
    rows: list[dict[str, Any]] = []
    competition = payload.get("competition", {}).get("code")
    for match in raw_rows:
        full_time = (match.get("score") or {}).get("fullTime") or {}
        season = match.get("season") or {}
        rows.append({
            "match_id": match.get("id"),
            "date": match.get("utcDate"),
            "home_team": (match.get("homeTeam") or {}).get("name"),
            "away_team": (match.get("awayTeam") or {}).get("name"),
            "home_goals": full_time.get("home"),
            "away_goals": full_time.get("away"),
            "status": match.get("status"),
            "competition": competition,
            "matchday": match.get("matchday"),
            "season": season.get("startDate", "")[:4] or payload.get("filters", {}).get("season"),
        })
    frame = pd.DataFrame(rows, columns=MATCH_COLUMNS)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce", utc=True)
    for column in ["match_id", "home_goals", "away_goals", "matchday", "season"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    frame[["home_goals", "away_goals"]] = frame[["home_goals", "away_goals"]].fillna(0)
    frame = frame.dropna(subset=["match_id", "home_team", "away_team", "date"])
    return _save_and_log(frame, json_path, output_dir, "matches", len(raw_rows))


def transform_standings(json_path: str | Path, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> pd.DataFrame:
    """Transform standings tables and save the result as Parquet."""
    payload = _read_json(json_path)
    tables = payload.get("standings") or []
    raw_rows = [row for table in tables for row in (table.get("table") or [])]
    season = _season_from(payload)
    season_info = payload.get("season") or {}
    matchday = season_info.get("currentMatchday") if isinstance(season_info, dict) else None
    rows = [{
        "position": row.get("position"),
        "team_name": (row.get("team") or {}).get("name"),
        "played": row.get("playedGames"),
        "won": row.get("won"),
        "drawn": row.get("draw"),
        "lost": row.get("lost"),
        "goals_for": row.get("goalsFor"),
        "goals_against": row.get("goalsAgainst"),
        "goal_diff": row.get("goalDifference"),
        "points": row.get("points"),
        "season": season,
        "matchday": matchday,
    } for row in raw_rows]
    frame = pd.DataFrame(rows, columns=STANDING_COLUMNS)
    numeric_columns = [column for column in STANDING_COLUMNS if column != "team_name"]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    frame[numeric_columns] = frame[numeric_columns].fillna(0)
    frame = frame.dropna(subset=["position", "team_name"])
    return _save_and_log(frame, json_path, output_dir, "standings", len(raw_rows))


def transform_scorers(json_path: str | Path, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> pd.DataFrame:
    """Transform scorer records and save the result as Parquet."""
    payload = _read_json(json_path)
    raw_rows = payload.get("scorers") or []
    season = _season_from(payload)
    rows = [{
        "player_name": (scorer.get("player") or {}).get("name"),
        "team": (scorer.get("team") or {}).get("name"),
        "goals": scorer.get("goals"),
        "assists": scorer.get("assists"),
        "penalties": scorer.get("penalties"),
        "season": season,
    } for scorer in raw_rows]
    frame = pd.DataFrame(rows, columns=SCORER_COLUMNS)
    for column in ["goals", "assists", "penalties", "season"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    frame[["goals", "assists", "penalties"]] = frame[["goals", "assists", "penalties"]].fillna(0)
    frame = frame.dropna(subset=["player_name", "team"])
    return _save_and_log(frame, json_path, output_dir, "scorers", len(raw_rows))


def parse_args() -> argparse.Namespace:
    """Parse paths for a manual transformation run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=REPOSITORY_ROOT / "data" / "raw")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    """Transform all recognized raw files in the input directory."""
    configure_logging()
    args = parse_args()
    files = sorted(args.input_dir.glob("*.json"))
    if not files:
        LOGGER.error("No JSON files found in %s", args.input_dir)
        return 1
    for path in files:
        if "_matches_" in path.name:
            transform_matches(path, args.output_dir)
        elif "_standings_" in path.name:
            transform_standings(path, args.output_dir)
        elif "_scorers_" in path.name:
            transform_scorers(path, args.output_dir)
        else:
            LOGGER.warning("Skipping unrecognized file: %s", path.name)
    LOGGER.info("Transformation completed for %d raw file(s)", len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
