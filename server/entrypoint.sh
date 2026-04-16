#!/usr/bin/env bash
set -euo pipefail

: "${SERVICE:=web}"
: "${DJANGO_SETTINGS_MODULE:=server.settings}"
export DJANGO_SETTINGS_MODULE

wait_for_migrations() {
  local timeout="${MIGRATION_WAIT_TIMEOUT:-120}"
  local interval="${MIGRATION_WAIT_INTERVAL:-3}"
  local elapsed=0

  echo "[entrypoint] Waiting for database migrations (timeout=${timeout}s)..."

  while (( elapsed < timeout )); do
    if python manage.py migrate --check --no-input >/dev/null 2>&1; then
      echo "[entrypoint] Migrations confirmed applied."
      return 0
    fi
    sleep "${interval}"
    elapsed=$(( elapsed + interval ))
  done

  echo "[entrypoint] ERROR: Migrations not applied after ${timeout}s. Exiting."
  exit 1
}

if [[ "${SERVICE}" == "worker" ]]; then
  wait_for_migrations
  echo "[entrypoint] Starting Dramatiq worker..."
  exec python -m dramatiq server.broker \
      --processes "${DRAM_PROCS:-2}" \
      --threads   "${DRAM_THREADS:-8}"

elif [[ "${SERVICE}" == "scheduler" ]]; then
  wait_for_migrations
  echo "[entrypoint] Starting task scheduler..."
  exec python manage.py run_scheduler

else
  if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
    echo "[entrypoint] Running migrations..."
    python manage.py migrate --noinput
  fi

  if [[ "${COLLECT_STATIC:-true}" == "true" ]]; then
    echo "[entrypoint] Collecting static files..."
    python manage.py collectstatic --noinput
  fi

  echo "[entrypoint] Starting Gunicorn (ASGI via UvicornWorker)..."
  : "${GUNICORN_CONF:=gunicorn.conf.py}"
  exec python -m gunicorn -c "${GUNICORN_CONF}" server.asgi:application
fi
