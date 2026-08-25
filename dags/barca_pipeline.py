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

TABLE_NAMES = ("matches", "standings", "scorers")

# Make the project scripts importable when Airflow runs inside its container.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _table_for(path: Path) -> str:
    """Map a raw or staging filename to its destination table.

    Filenames follow the pattern <competition>_<resource>_<timestamp>, so the
    resource name identifies the table. Raising here rather than returning None
    means an unexpected file fails the task instead of being skipped silently.
    """
    for table_name in TABLE_NAMES:
        if f"_{table_name}_" in path.name:
            return table_name
    raise ValueError(f"Unrecognized file, cannot map it to a table: {path.name}")


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
    transformers = {
        "matches": transform_matches,
        "standings": transform_standings,
        "scorers": transform_scorers,
    }
    for raw_path in extracted["files"]:
        path = Path(raw_path)
        frame = transformers[_table_for(path)](path, STAGING_DIR)
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

    import pyarrow.parquet as pq

    loaded_rows = 0
    for parquet_path in transformed["files"]:
        path = Path(parquet_path)
        table_name = _table_for(path)
        if not load_to_postgres(path, table_name, "upsert"):
            raise RuntimeError(f"Load failed for {path.name}")

        # Parquet stores the row count in its footer metadata, so this reads a
        # few bytes instead of decoding the whole file just to call len() on it.
        loaded_rows += pq.ParquetFile(path).metadata.num_rows

    metrics = {"loaded_rows": loaded_rows}
    LOGGER.info("Load completed: %s", metrics)
    return metrics


def validate_staging_task(**context: Any) -> None:
    """Block the pipeline when a staging dataset is unfit to load.

    This runs before load_task so bad data never reaches PostgreSQL. The same
    validation also runs inside load_to_postgres as a library-level safety net,
    but having it as its own task makes the gate visible in the DAG graph and
    stops the run before a database connection is ever opened.
    """
    import pandas as pd
    from load import validate_quality

    transformed = context["ti"].xcom_pull(task_ids="transform_task")
    if not transformed or not transformed.get("files"):
        raise ValueError("No transformed files available for quality checks")

    for parquet_path in transformed["files"]:
        path = Path(parquet_path)
        table_name = _table_for(path)
        if not validate_quality(pd.read_parquet(path), table_name):
            raise ValueError(f"Staging validation failed for {path.name}")
    LOGGER.info("All staging datasets passed validation")


def verify_warehouse_task(**context: Any) -> None:
    """Assert that the warehouse is coherent after the load.

    Unlike validate_staging_task, these checks query PostgreSQL itself. They
    cover freshness, primary-key integrity and football business rules -- the
    kind of problem that only becomes visible once rows from several runs sit
    in the same table.
    """
    from load import get_connection
    from quality import run_warehouse_checks

    connection = get_connection()
    try:
        failures = run_warehouse_checks(connection)
    finally:
        connection.close()

    if failures:
        raise ValueError(f"Warehouse checks failed: {', '.join(failures)}")
    LOGGER.info("All warehouse checks passed")


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


def alert_on_failure(context: dict[str, Any]) -> None:
    """Emit a structured alert once a task has exhausted all of its retries.

    Airflow fires on_retry_callback between attempts and this callback only when
    the task reaches its final failed state, so the alert marks a real incident
    rather than a transient hiccup. Logging keeps the project dependency-free;
    swapping in Slack or email means changing only this function.
    """
    task_instance = context.get("task_instance")
    LOGGER.error(
        "PIPELINE FAILURE | dag=%s task=%s run=%s attempts=%s | %s",
        context.get("dag").dag_id if context.get("dag") else "unknown",
        task_instance.task_id if task_instance else "unknown",
        context.get("run_id"),
        task_instance.try_number - 1 if task_instance else "unknown",
        context.get("exception"),
    )


default_args = {
    "owner": "samuel",

    # Three attempts total. The extract step talks to an external API over the
    # network, which is the single most likely source of transient failure.
    "retries": 2,

    # Wait before retrying instead of hammering a service that is already
    # struggling. Five minutes also outlasts the API's per-minute rate window.
    "retry_delay": timedelta(minutes=5),

    # Back off progressively: 5 minutes, then 10, capped by max_retry_delay.
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),

    # A hung HTTP request would otherwise hold a scheduler slot indefinitely.
    "execution_timeout": timedelta(minutes=30),

    "on_failure_callback": alert_on_failure,
}

# Retrying a data-quality failure is wasted time: the same data produces the
# same verdict on every attempt. These two tasks fail fast and stay failed.
QUALITY_GATE_ARGS = {"retries": 0, "retry_delay": timedelta(seconds=0)}

with DAG(
    dag_id="barca_etl_pipeline",
    default_args=default_args,
    schedule="@daily",

    # start_date must be a fixed point in time. A value derived from
    # datetime.now() changes on every DAG parse, which happens every few
    # seconds, leaving the scheduler unable to settle on the next run.
    start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),

    # Do not backfill the runs between start_date and today.
    catchup=False,

    # Prevent a slow run from overlapping with the next scheduled one and
    # writing to the same tables concurrently.
    max_active_runs=1,

    # Cap the whole pipeline, not just individual tasks.
    dagrun_timeout=timedelta(hours=2),

    tags=["barca", "football", "etl"],
) as dag:
    extract = PythonOperator(task_id="extract_task", python_callable=extract_task)
    transform = PythonOperator(task_id="transform_task", python_callable=transform_task)
    validate_staging = PythonOperator(
        task_id="validate_staging_task", python_callable=validate_staging_task, **QUALITY_GATE_ARGS
    )
    load = PythonOperator(task_id="load_task", python_callable=load_task)
    verify_warehouse = PythonOperator(
        task_id="verify_warehouse_task", python_callable=verify_warehouse_task, **QUALITY_GATE_ARGS
    )
    notification = PythonOperator(task_id="notification_task", python_callable=notification_task)

    # Validation sits before the load so bad data never reaches PostgreSQL, and
    # verification sits after it so the warehouse is checked as it actually is.
    extract >> transform >> validate_staging >> load >> verify_warehouse >> notification
