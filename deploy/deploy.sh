#!/usr/bin/env bash
# =============================================================================
# Roll a new release onto the gateway.
#
# Runs on the server, invoked over SSH by CI or by hand. It swaps the images,
# verifies the result, and puts the previous release back if the verification
# fails.
#
#   ./deploy.sh sha-a1b2c3d     pull that release from the registry
#   ./deploy.sh local --build   build here from the current checkout
#
# Pulling is the normal path. Compiling the traffic module takes minutes and
# can fail, and a gateway is the wrong place to discover a build error -- so CI
# builds once and every server pulls the artefact that was tested.
#
# --build exists for the case before any of that is wired up, and for the day
# the registry is unreachable and you need a release out regardless.
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cloudbalancer}"
TAG="${1:-}"
BUILD_LOCALLY=false
STATE="${APP_DIR}/.deployed-tag"

[[ "${2:-}" == "--build" ]] && BUILD_LOCALLY=true

if [[ -z "$TAG" ]]; then
    cat >&2 <<'USAGE'
usage: deploy.sh <image-tag> [--build]

  deploy.sh sha-a1b2c3d      pull that release from the registry
  deploy.sh local --build    build here from the current checkout
USAGE
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
# Get the images ready first. This is the slow part and nothing is disturbed
# while it runs, so the window where the gateway is mid-change stays short.
# ---------------------------------------------------------------------------
if [[ "$BUILD_LOCALLY" == true ]]; then
    log "Building images here (this compiles the traffic module -- give it a few minutes)"
    compose "$TAG" build nginx django panel
else
    log "Pulling images"
    if ! compose "$TAG" pull --quiet nginx django celery celery-beat panel; then
        cat >&2 <<EOF

Could not pull ${TAG}.

  * If CI has not pushed this release yet, check the Actions tab.
  * If the packages are private, this host needs to authenticate:
        echo <token> | docker login ghcr.io -u <user> --password-stdin
  * To build here instead, without the registry:
        ./deploy.sh local --build

EOF
        exit 1
    fi
fi

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
if [[ "$previous" != "local" ]]; then
    compose "$previous" pull --quiet nginx django celery celery-beat panel
fi
compose "$previous" up -d --no-build --remove-orphans

if verify; then
    log "Rolled back to ${previous}"
    exit 1
fi

echo "::error::Rollback did not verify either. The gateway needs attention by hand." >&2
exit 1
