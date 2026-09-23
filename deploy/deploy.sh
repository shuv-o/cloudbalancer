#!/usr/bin/env bash
# =============================================================================
# Roll a new release onto the gateway.
#
# Runs on the server, invoked over SSH by CI or by hand. Images are already
# built and pushed; this pulls them, swaps them in, verifies the result, and
# puts the previous release back if the verification fails.
#
# Production never builds. Compiling the traffic module takes minutes and can
# fail, and a gateway is the wrong place to discover a build error.
#
#   ./deploy.sh <image-tag>
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cloudbalancer}"
TAG="${1:-}"
STATE="${APP_DIR}/.deployed-tag"

if [[ -z "$TAG" ]]; then
    echo "usage: $0 <image-tag>" >&2
    exit 2
fi

cd "$APP_DIR"

compose() {
    IMAGE_TAG="$1" docker compose "${@:2}"
}

log() { printf '\n\033[36m==>\033[0m %s\n' "$*"; }

previous=""
[[ -f "$STATE" ]] && previous="$(cat "$STATE")"

log "Deploying ${TAG}${previous:+ (currently ${previous})}"

# ---------------------------------------------------------------------------
# Fetch first. Pulling is the slow part and nothing is disturbed while it runs,
# so the window where the gateway is mid-change stays as short as possible.
# ---------------------------------------------------------------------------
log "Pulling images"
compose "$TAG" pull --quiet nginx django celery celery-beat panel

# ---------------------------------------------------------------------------
# Migrations before the new code serves anything. The django service runs them
# on start, but running them here first means a migration failure stops the
# deploy rather than leaving a container restart-looping.
# ---------------------------------------------------------------------------
log "Starting the data stores"
compose "$TAG" up -d postgres redis

log "Applying migrations"
# No --no-deps: compose then honours depends_on and waits for Postgres to
# report healthy, which on a first deploy is the difference between migrating
# and failing to connect to a database that was never started.
compose "$TAG" run --rm django python manage.py migrate --noinput

log "Rebuilding the control panel bundle"
compose "$TAG" up --no-build --force-recreate panel

log "Starting services"
compose "$TAG" up -d --no-build --remove-orphans

# ---------------------------------------------------------------------------
# Verify. A container that started is not the same as a gateway that works.
# ---------------------------------------------------------------------------
verify() {
    # Up to three minutes. A first deploy applies every migration and collects
    # static files before gunicorn binds, and reporting success before that
    # finishes is how a deploy "succeeds" into a panel that answers 502.
    local waited=0
    for _ in $(seq 1 90); do
        if gateway_ok && control_plane_ok; then
            return 0
        fi
        waited=$((waited + 2))
        if (( waited % 30 == 0 )); then
            echo "  still waiting (${waited}s): $(state_summary)"
        fi
        sleep 2
    done
    return 1
}

gateway_ok() {
    docker compose exec -T nginx nginx -t >/dev/null 2>&1 \
        && curl -fsS --max-time 3 http://localhost/health >/dev/null 2>&1
}

control_plane_ok() {
    # The panel is unusable until this answers, so a deploy is not finished
    # until it does.
    [[ "$(docker inspect -f '{{.State.Health.Status}}' cloudbalancer-django 2>/dev/null)" == "healthy" ]]
}

state_summary() {
    printf 'nginx=%s django=%s' \
        "$(docker inspect -f '{{.State.Status}}' cloudbalancer-nginx 2>/dev/null || echo absent)" \
        "$(docker inspect -f '{{.State.Health.Status}}' cloudbalancer-django 2>/dev/null || echo absent)"
}

log "Verifying"
if verify; then
    echo "$TAG" > "$STATE"
    log "Deployed ${TAG}"

    # Re-render from the database: a release may change how config is generated
    # even when nothing in the database changed.
    compose "$TAG" exec -T django python manage.py deploy_config || true

    docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true
    exit 0
fi

# ---------------------------------------------------------------------------
# Rollback.
# ---------------------------------------------------------------------------
echo "::error::Verification failed after deploying ${TAG}" >&2
echo "State: $(state_summary)" >&2
docker compose logs --tail 60 django nginx >&2 || true

if [[ -z "$previous" ]]; then
    echo "No previous release recorded, so there is nothing to roll back to." >&2
    echo "The gateway is serving whatever it last loaded; fix forward." >&2
    exit 1
fi

log "Rolling back to ${previous}"
compose "$previous" pull --quiet nginx django celery celery-beat panel
compose "$previous" up -d --no-build --remove-orphans

if verify; then
    log "Rolled back to ${previous}"
    exit 1
fi

echo "::error::Rollback did not verify either. The gateway needs attention by hand." >&2
exit 1
