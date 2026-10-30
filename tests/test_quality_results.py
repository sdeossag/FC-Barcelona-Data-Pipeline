"""Unit tests for warehouse check evaluation and persistence.

These do not need PostgreSQL: the checks are pure queries plus a predicate, so a
cursor that returns scripted values exercises the same decision path the
pipeline takes.
"""

from scripts.quality import CheckResult, WarehouseCheck, evaluate_checks, run_warehouse_checks


CHECKS = (
    WarehouseCheck(
        name="positive",
        sql="SELECT 1",
        passes=lambda value: value > 0,
        expectation="value must be positive",
    ),
    WarehouseCheck(
        name="zero",
        sql="SELECT 0",
        passes=lambda value: value == 0,
        expectation="value must be zero",
    ),
)


class _Cursor:
    """Minimal cursor that returns one scripted value per execution."""

    def __init__(self, values):
        self._values = iter(values)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement):
        return None

    def fetchone(self):
        return (next(self._values),)


class _Connection:
    """Minimal connection exposing the cursor context manager under test."""

    def __init__(self, values):
        self._cursor = _Cursor(values)

    def cursor(self):
        return self._cursor


def test_evaluate_checks_reports_every_check():
    """Each check yields one result, including the value that decided it."""
    results = evaluate_checks(_Connection([3, 0]), CHECKS)

    assert results == [
        CheckResult("positive", True, 3, "value must be positive"),
        CheckResult("zero", True, 0, "value must be zero"),
    ]


def test_evaluate_checks_keeps_the_observed_value_on_failure():
    """A failing check still records its value, so the report can explain it."""
    results = evaluate_checks(_Connection([0, 1]), CHECKS)

    assert [result.passed for result in results] == [False, False]
    assert [result.observed_value for result in results] == [0, 1]


def test_run_warehouse_checks_returns_only_failures():
    """The pipeline gate returns names, not the full result set."""
    failures = run_warehouse_checks(_Connection([0, 1]), CHECKS, persist=False)

    assert failures == ["positive", "zero"]
