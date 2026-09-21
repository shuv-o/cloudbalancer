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

WORKDIR /app

# The Docker CLI alone, not the engine: this container's only use for it is
# `docker exec nginx nginx -s reload`, so pulling in a whole daemon would be
# several hundred megabytes for one command.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev curl ca-certificates \
    && ARCH="$(uname -m)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4"]
