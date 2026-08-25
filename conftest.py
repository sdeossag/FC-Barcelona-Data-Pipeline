"""Pytest configuration applied before any test module is imported.

The project's modules are imported two different ways, and both are correct in
their own context:

* Airflow puts /opt/airflow/scripts on sys.path and the DAG imports flat names
  (``from load import ...``), which is how the scripts import each other.
* The test suite imports them as a package (``from scripts.load import ...``)
  so test files stay unambiguous about what they are exercising.

Adding scripts/ to sys.path here makes the flat imports inside those modules
resolve during a test run too. pytest loads the root conftest.py before it
collects any test, so this happens early enough to matter.
"""

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"

for path in (REPOSITORY_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
