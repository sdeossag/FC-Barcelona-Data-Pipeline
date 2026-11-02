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
# re-runs pip when the requirements themselves change.
COPY requirements.txt requirements-ml.txt /tmp/

# Install as the unprivileged airflow user so the packages land in the same
# site-packages the scheduler and webserver import from at runtime.
USER airflow

RUN pip install --no-cache-dir \
      --constraint "${CONSTRAINTS_URL}" \
      --requirement /tmp/requirements.txt

# The prediction DAG imports scikit-learn and xgboost, so they belong in this
# image too -- and under the same constraints as everything else.
#
# Installing them unconstrained breaks the image. None of them is named in the
# constraints file, but numpy is, at 1.24.4, and a free resolution pulls in
# numpy 2.x to satisfy a recent scikit-learn. pandas 2.1.4 in this image is
# compiled against the 1.x ABI, so every import then fails with "numpy.dtype
# size changed". Passing the constraints pins numpy and lets the packages that
# are absent from it resolve freely, which is exactly what is wanted.
RUN pip install --no-cache-dir \
      --constraint "${CONSTRAINTS_URL}" \
      --requirement /tmp/requirements-ml.txt
