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

ARG DOCKER_CLI_VERSION=27.5.1
# Must match the PostgreSQL major version in docker-compose.yml. pg_dump
# refuses to dump a server newer than itself, and the failure surfaces as a
# backup that has been quietly failing for however long nobody checked.
ARG POSTGRES_MAJOR=16

WORKDIR /app

# The Docker CLI alone, not the engine: this container's only use for it is
# `docker exec nginx nginx -s reload`, so pulling in a whole daemon would be
# several hundred megabytes for one command.
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
    && ARCH="$(uname -m)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]
