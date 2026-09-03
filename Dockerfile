FROM python:3.11-slim

WORKDIR /app

# System deps for building pysui/solders wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App (Telegram bot + internal API; the web frontend is intentionally excluded)
COPY service/ ./service/

WORKDIR /app/service

# No EXPOSE / no public port — this is a background Telegram bot + its
# internal API on 127.0.0.1:8000 (used by live_agent for prices/positions),
# NOT a web application. Coolify must treat it as a worker.
CMD ["python", "start.py"]
