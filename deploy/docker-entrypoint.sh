#!/bin/sh
# Fail before importing the app, so the first log line is an actionable sentence
# rather than a pydantic traceback forty frames deep.
set -eu

if [ "${MASSINGPLAN_ENV:-production}" = "production" ] && [ -z "${MASSINGPLAN_SECRET_KEY:-}" ]; then
  echo "MASSINGPLAN_SECRET_KEY is not set." >&2
  echo "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"" >&2
  echo "Four workers each generating their own key invalidate each other's" >&2
  echo "sessions, and the symptom is users being logged out at random." >&2
  exit 1
fi

if [ "${MASSINGPLAN_SKIP_MIGRATIONS:-0}" != "1" ]; then
  # Retry. Postgres accepts TCP connections several seconds before it will
  # serve a query, so a single attempt at container start loses the race
  # roughly one boot in five and the container restart-loops.
  attempt=1
  until alembic upgrade head; do
    if [ "$attempt" -ge 15 ]; then
      echo "the database did not become ready after $attempt attempts" >&2
      exit 1
    fi
    echo "database not ready yet (attempt $attempt); retrying in 2s" >&2
    attempt=$((attempt + 1))
    sleep 2
  done
fi

# A worker pool should not race the migration. Set MASSINGPLAN_SKIP_MIGRATIONS=1
# on every replica but one, or run the migration as a separate job.
exec "$@"
