#!/bin/sh
# run the migrations then start gunicorn. exec makes gunicorn pid 1
# so docker stop kills it properly
set -e
python manage.py migrate --noinput
exec gunicorn summit_map_project.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 4 \
    --timeout 60
