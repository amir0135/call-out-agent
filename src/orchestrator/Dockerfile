FROM python:3.11-slim

# curl for healthchecks; gcc + python3-dev needed to build psutil from source on aarch64
# (azure-monitor-opentelemetry-exporter pins psutil~=5.9 which has no linux/aarch64 wheel).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY src/orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and SOP definitions in one layer
COPY src/orchestrator/ /app/src/orchestrator/
COPY sops/ /app/sops/
COPY config/ /app/config/

ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000"]
