FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY src/agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/agent/ /app/

EXPOSE 8080

CMD ["uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]
