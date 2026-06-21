FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY push_relay ./push_relay

# Persistent SQLite lives on a mounted volume in prod (see fly.toml). Override
# via HERMES_RELAY_DATABASE_PATH. Postgres is recommended at real scale.
ENV HERMES_RELAY_DATABASE_PATH=/data/push-relay.db

EXPOSE 8080
# Honor $PORT (Fly/Render/Railway inject it); default 8080.
CMD ["sh", "-c", "uvicorn push_relay.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
