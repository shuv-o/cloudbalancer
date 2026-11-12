# =============================================================================
# Management plane: Django REST API, Celery workers, certbot.
#
# Never on the request path. It renders Nginx configuration, asks Nginx to
# reload, and talks to certificate authorities -- all of which can take as long
# as they need without costing a single proxied request any latency.
# =============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Must match the PostgreSQL major version in docker-compose.yml. pg_dump
# refuses to dump a server newer than itself, and the failure surfaces as a
# backup that has been quietly failing for however long nobody checked.
ARG POSTGRES_MAJOR=16

WORKDIR /app

# No Docker CLI any more. Its only use was `docker exec nginx nginx -s
# reload`, which needed /var/run/docker.sock mounted in -- the unauthenticated
# Docker API, and therefore root on the host, in exchange for sending two
# signals. The Nginx image now runs a watcher that answers those two commands
# through a shared directory instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-client-${POSTGRES_MAJOR}" \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# The lock, not the ranges. requirements.txt says what this project depends
# on; requirements.lock says exactly which versions and which file contents,
# so two builds of the same commit install the same bytes. --require-hashes
# makes a tampered or substituted package a build failure rather than a
# silent swap.
COPY requirements.txt requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY . .
RUN chmod +x docker-entrypoint.sh

# Nothing in the management plane needs root. It renders templates, talks to
# Postgres, runs certbot and writes to volumes.
#
# The uid is fixed rather than left to the distribution, because every volume
# this container writes to has to be owned by it, and a uid that moved between
# image rebuilds would silently make those volumes unwritable.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" cloudbalancer \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin cloudbalancer \
    && mkdir -p /etc/nginx/conf.d /etc/nginx/ssl /etc/letsencrypt \
                /var/lib/letsencrypt /var/log/letsencrypt /var/www/acme \
                /var/cache/nginx /var/backups/cloudbalancer \
                /var/lib/letsencrypt/dns-credentials \
                /var/run/cloudbalancer /app/staticfiles \
    # A named volume created fresh inherits the ownership of the image
    # directory behind it, so these chowns are what make a clean install work
    # without any manual step. A volume that already exists keeps the
    # ownership it has, which is what `make fix-permissions` is for.
    && chown -R "${APP_UID}:${APP_GID}" \
        /app /etc/nginx/conf.d /etc/nginx/ssl /etc/letsencrypt \
        /var/lib/letsencrypt /var/log/letsencrypt /var/www/acme \
        /var/cache/nginx /var/backups/cloudbalancer /var/run/cloudbalancer

USER cloudbalancer

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]
