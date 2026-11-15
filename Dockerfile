# Neko web app: weekly calls, desk, forex planner, console and public page.
# This is the default deployment. The old Telegram bot stack is Dockerfile.telegram.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY service/ ./service/
COPY web/ ./web/
COPY skills/ ./skills/
COPY research/exports/tables/live_engine_replay.json ./research/exports/tables/live_engine_replay.json
COPY research/exports/tables/crypto_weekly_search.json ./research/exports/tables/crypto_weekly_search.json
COPY research/exports/tables/crypto_in_or_out.json ./research/exports/tables/crypto_in_or_out.json
COPY research/exports/tables/leverage_on_rule.json ./research/exports/tables/leverage_on_rule.json
COPY research/exports/tables/crypto_long_short.json ./research/exports/tables/crypto_long_short.json

# Persist this directory: it holds the database and the secret key.
# Set NEKO_PASSWORD when running; nothing else is required.
ENV NEKO_DATA_DIR=/data
VOLUME /data

EXPOSE 8000
CMD ["python", "service/start_web.py"]
