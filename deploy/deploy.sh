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
log "Applying migrations"
compose "$TAG" run --rm --no-deps django python manage.py migrate --noinput

log "Rebuilding the control panel bundle"
compose "$TAG" up --no-build --force-recreate panel

log "Starting services"
compose "$TAG" up -d --no-build --remove-orphans

# ---------------------------------------------------------------------------
# Verify. A container that started is not the same as a gateway that works.
# ---------------------------------------------------------------------------
verify() {
    # Up to a minute: Postgres may be applying migrations and Nginx needs a
    # moment to bind after a recreate.
    for _ in $(seq 1 30); do
        if docker compose exec -T nginx nginx -t >/dev/null 2>&1 \
           && curl -fsS --max-time 3 http://localhost/health >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
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
docker compose logs --tail 40 nginx django >&2 || true

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
