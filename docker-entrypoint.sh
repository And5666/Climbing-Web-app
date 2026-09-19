#!/bin/sh
# Migrate the (volume-backed) database, then serve. exec keeps
# gunicorn as PID 1 so `docker stop` shuts it down cleanly.
set -e
python manage.py migrate --noinput
exec gunicorn summit_map_project.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 4 \
    --timeout 60
