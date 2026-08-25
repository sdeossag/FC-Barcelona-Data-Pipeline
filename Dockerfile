# ============================================================
# Custom Airflow image for the Barca data pipeline
# ============================================================
# Building dependencies into an image replaces _PIP_ADDITIONAL_REQUIREMENTS,
# which reinstalled every package on each container start. That made startups
# slow and left the pipeline exposed to upstream releases and PyPI outages.

# Airflow 2.8.4 supports Python 3.8 through 3.11. We pin 3.11 explicitly:
# the default tag ships Python 3.8, which reached end of life in October 2024
# and caps pandas at 2.0.x.
FROM apache/airflow:2.8.4-python3.11

# Airflow publishes a constraints file for each version/Python pair listing the
# exact versions of its ~600 transitive dependencies that are known to work
# together. Installing against it stops pip from upgrading one of them and
# leaving Airflow in an inconsistent state.
ARG AIRFLOW_VERSION=2.8.4
ARG PYTHON_VERSION=3.11
ARG CONSTRAINTS_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# Copy the requirements before installing so Docker caches this layer and only
# re-runs pip when requirements.txt itself changes.
COPY requirements.txt /tmp/requirements.txt

# Install as the unprivileged airflow user so the packages land in the same
# site-packages the scheduler and webserver import from at runtime.
USER airflow

RUN pip install --no-cache-dir \
      --constraint "${CONSTRAINTS_URL}" \
      --requirement /tmp/requirements.txt
