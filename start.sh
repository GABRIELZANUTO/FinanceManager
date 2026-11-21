#!/bin/sh
# Start on Discloud: install dependencies, create or update the database (SQLite, made on the
# server by migrate), collect static files, then serve on 0.0.0.0:8080 as Discloud requires.
set -e
PY=$(command -v python3 || command -v python)

"$PY" -m pip install --quiet --no-cache-dir -r requirements.txt
"$PY" manage.py migrate --noinput
"$PY" manage.py collectstatic --noinput --clear --verbosity 0
# Post due recurring items and purge the trash (there is no cron on Discloud).
"$PY" manage.py apply_recurring || true

# One worker keeps SQLite and the in-memory login throttle consistent; threads handle concurrency.
exec "$PY" -m gunicorn expensemanager.wsgi:application \
  --bind 0.0.0.0:8080 --workers 1 --threads 4 --timeout 180 \
  --access-logfile - --error-logfile -
