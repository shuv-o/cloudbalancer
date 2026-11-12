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

# -----------------------------------------------------------------------------
# Writability, checked before anything else.
#
# This container stopped running as root. A volume created before that change
# is still owned by root, and the failure shows up far from its cause: certbot
# reporting a permission error on renewal weeks later, or a deploy writing a
# config it cannot replace. None of those name the actual problem.
#
# So it is named here, once, with the command that fixes it.
# -----------------------------------------------------------------------------
unwritable=""
for directory in \
    /etc/nginx/conf.d \
    /etc/nginx/ssl \
    /etc/letsencrypt \
    /var/www/acme \
    /var/cache/nginx \
    /var/backups/cloudbalancer \
    /var/run/cloudbalancer
do
    [ -d "$directory" ] || continue
    if ! touch "$directory/.writable-probe" 2>/dev/null; then
        unwritable="$unwritable $directory"
    else
        rm -f "$directory/.writable-probe"
    fi
done

if [ -n "$unwritable" ]; then
    echo "ERROR: this container runs as uid $(id -u) and cannot write to:" >&2
    for directory in $unwritable; do
        echo "         $directory" >&2
    done
    cat >&2 <<'MESSAGE'

These volumes were created while the management plane ran as root. Nothing is
broken; they just need to belong to the user that now writes to them. On the
host, once:

    make fix-permissions

Then start again. A clean install never sees this: a volume created fresh
inherits ownership from the image, which already has it right.
MESSAGE
    exit 1
fi

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
