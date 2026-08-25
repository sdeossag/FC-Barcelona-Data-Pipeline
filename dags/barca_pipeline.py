"""Daily Airflow orchestration for the Barcelona football data pipeline."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator


LOGGER = logging.getLogger("barca_etl_pipeline")
AIRFLOW_ROOT = Path("/opt/airflow")
SCRIPTS_DIR = AIRFLOW_ROOT / "scripts"
RAW_DIR = AIRFLOW_ROOT / "data" / "raw"
STAGING_DIR = AIRFLOW_ROOT / "data" / "staging"

# Make the project scripts importable when Airflow runs inside its container.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def extract_task(**context: Any) -> dict[str, Any]:
    """Extract both supported competitions and return raw-file metrics."""
    from extract import get_matches, get_scorers, get_standings, load_dotenv, save_raw_response

    load_dotenv(AIRFLOW_ROOT / ".env")
    api_key = os.getenv("API_KEY_FOOTBALL")
    if not api_key or api_key == "your_api_key_here":
        raise ValueError("API_KEY_FOOTBALL is not configured for Airflow")

    files: list[str] = []
    extracted_rows = 0
    for competition in ("PD", "CL"):
        for resource_name, extractor in (
            ("matches", get_matches),
            ("standings", get_standings),
            ("scorers", get_scorers),
        ):
            payload = extractor(competition, api_key)
            output_path = save_raw_response(payload, f"{competition.lower()}_{resource_name}", RAW_DIR)
            files.append(str(output_path))
            records = payload.get(resource_name, [])
            if resource_name == "standings":
                records = [row for table in records for row in (table.get("table") or [])]
            extracted_rows += len(records)

    metrics = {"files": files, "extracted_rows": extracted_rows}
    LOGGER.info("Extraction completed: %s", metrics)
    return metrics


def transform_task(**context: Any) -> dict[str, Any]:
    """Transform extracted JSON files into Parquet and return row metrics."""
    from transform import transform_matches, transform_scorers, transform_standings

    extracted = context["ti"].xcom_pull(task_ids="extract_task")
    if not extracted or not extracted.get("files"):
        raise ValueError("No extracted files were returned by extract_task")

    parquet_files: list[str] = []
    transformed_rows = 0
    for raw_path in extracted["files"]:
        path = Path(raw_path)
        if "_matches_" in path.name:
            frame = transform_matches(path, STAGING_DIR)
        elif "_standings_" in path.name:
            frame = transform_standings(path, STAGING_DIR)
        elif "_scorers_" in path.name:
            frame = transform_scorers(path, STAGING_DIR)
        else:
            raise ValueError(f"Unrecognized raw file: {path.name}")
        parquet_path = STAGING_DIR / f"{path.stem}.parquet"
        parquet_files.append(str(parquet_path))
        transformed_rows += len(frame)

    metrics = {"files": parquet_files, "transformed_rows": transformed_rows}
    LOGGER.info("Transformation completed: %s", metrics)
    return metrics


def load_task(**context: Any) -> dict[str, Any]:
    """Load every transformed dataset with idempotent upsert behavior."""
    from load import load_to_postgres

    transformed = context["ti"].xcom_pull(task_ids="transform_task")
    if not transformed or not transformed.get("files"):
        raise ValueError("No transformed files were returned by transform_task")

    loaded_rows = 0
    for parquet_path in transformed["files"]:
        path = Path(parquet_path)
        table_name = next((table for table in ("matches", "standings", "scorers") if f"_{table}_" in path.name), None)
        if table_name is None:
            raise ValueError(f"Unrecognized staging file: {path.name}")
        if not load_to_postgres(path, table_name, "upsert"):
            raise RuntimeError(f"Load failed for {path.name}")
        import pandas as pd

        loaded_rows += len(pd.read_parquet(path))

    metrics = {"loaded_rows": loaded_rows}
    LOGGER.info("Load completed: %s", metrics)
    return metrics


def quality_check_task(**context: Any) -> None:
    """Re-run data-quality checks on every staging dataset before notification."""
    from load import validate_quality

    transformed = context["ti"].xcom_pull(task_ids="transform_task")
    if not transformed or not transformed.get("files"):
        raise ValueError("No transformed files available for quality checks")
    for parquet_path in transformed["files"]:
        path = Path(parquet_path)
        table_name = next((table for table in ("matches", "standings", "scorers") if f"_{table}_" in path.name), None)
        if table_name is None:
            raise ValueError(f"Unrecognized staging file: {path.name}")
        import pandas as pd

        if not validate_quality(pd.read_parquet(path), table_name):
            raise ValueError(f"Quality check failed for {path.name}")
    LOGGER.info("All quality checks passed")


def notification_task(**context: Any) -> None:
    """Log final pipeline metrics after all previous tasks succeed."""
    extracted = context["ti"].xcom_pull(task_ids="extract_task") or {}
    transformed = context["ti"].xcom_pull(task_ids="transform_task") or {}
    loaded = context["ti"].xcom_pull(task_ids="load_task") or {}
    LOGGER.info(
        "Pipeline completed successfully | extracted_rows=%s transformed_rows=%s loaded_rows=%s",
        extracted.get("extracted_rows", 0),
        transformed.get("transformed_rows", 0),
        loaded.get("loaded_rows", 0),
    )


default_args = {"owner": "samuel", "retries": 0}

with DAG(
    dag_id="barca_etl_pipeline",
    default_args=default_args,
    schedule="@daily",
    start_date=datetime.now(timezone.utc) - timedelta(days=7),
    catchup=False,
    tags=["barca", "football", "etl"],
) as dag:
    extract = PythonOperator(task_id="extract_task", python_callable=extract_task)
    transform = PythonOperator(task_id="transform_task", python_callable=transform_task)
    load = PythonOperator(task_id="load_task", python_callable=load_task)
    quality_check = PythonOperator(task_id="quality_check_task", python_callable=quality_check_task)
    notification = PythonOperator(task_id="notification_task", python_callable=notification_task)

    extract >> transform >> load >> quality_check >> notification
