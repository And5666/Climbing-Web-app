FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Non-root from the start: everything below runs as appuser.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/staticfiles /data \
    && chown -R appuser:appuser /app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

# Hashed/compressed static files are built into the image. A dummy key
# is enough here: collectstatic never signs anything with it.
RUN DJANGO_DEBUG=False DJANGO_SECRET_KEY=build-only-dummy-key \
    python manage.py collectstatic --noinput

USER appuser
EXPOSE 8000
VOLUME /data

CMD ["sh", "/app/docker-entrypoint.sh"]
