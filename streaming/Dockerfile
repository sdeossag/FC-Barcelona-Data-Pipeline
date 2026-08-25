# ============================================================
# Image for the Kafka producer and consumer
# ============================================================
# The streaming services are packaged so the whole architecture comes up with a
# single docker compose command, and so they resolve the same service hostnames
# the rest of the stack uses. Running them from the host would need PostgreSQL
# and Kafka addresses that differ from the ones in .env.

FROM python:3.11-slim

# Never write .pyc files, and never buffer stdout: logs from a container should
# appear as they happen rather than when the buffer fills.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements before the code so this layer is cached and pip only runs
# again when the dependencies actually change.
COPY requirements-streaming.txt /tmp/requirements-streaming.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements-streaming.txt

# Run as an unprivileged user rather than root.
RUN useradd --create-home --uid 1000 streaming
USER streaming

# The source is bind-mounted by docker-compose, so edits are picked up on the
# next run without rebuilding the image.
CMD ["python", "streaming/consumer.py"]
