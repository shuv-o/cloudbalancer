#!/bin/sh
# =============================================================================
# Start the control plane.
#
# A script rather than a command in the compose file, because this sequence
# was previously written as a YAML folded scalar and YAML folds only the lines
# at the first line's indentation. The continuation lines were indented
# further, so their newlines survived into the string the shell was given --
# and the shell read `gunicorn config.wsgi:application` as a complete command.
# Gunicorn then bound its default address, 127.0.0.1, which no other container
# can reach. The panel answered 503 and the logs showed a healthy start.
#
# Nothing in here is clever. That is the point.
# =============================================================================
set -eu

echo "==> Applying migrations"
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Bootstrapping"
python manage.py bootstrap --skip-deploy

# 0.0.0.0, not localhost: Nginx connects from a different container.
BIND="${GUNICORN_BIND:-0.0.0.0:8000}"
WORKERS="${GUNICORN_WORKERS:-3}"
THREADS="${GUNICORN_THREADS:-4}"

echo "==> Starting gunicorn on ${BIND}: ${WORKERS} workers x ${THREADS} threads"

# exec so gunicorn becomes PID 1 and receives stop signals directly, instead
# of the shell swallowing them and the container being killed after a timeout.
# gthread rather than the default sync worker. Every slow thing this process
# does is a wait on something else -- Postgres, Redis, Nginx's status
# endpoint, a certificate authority. A sync worker blocked on one of those
# serves no other request, and with four of them a single unreachable
# dependency could stall the whole control plane.
exec gunicorn config.wsgi:application \
    --bind "${BIND}" \
    --worker-class gthread \
    --workers "${WORKERS}" \
    --threads "${THREADS}" \
    --timeout 120 \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile -
