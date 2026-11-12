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
NO_CACHE=false
STATE="${APP_DIR}/.deployed-tag"

for arg in "${@:2}"; do
    case "$arg" in
        --build)    BUILD_LOCALLY=true ;;
        --no-cache) NO_CACHE=true ;;
        *)          echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ -z "$TAG" ]]; then
    cat >&2 <<'USAGE'
usage: deploy.sh <image-tag> [--build] [--no-cache]

  deploy.sh sha-a1b2c3d                 pull that release from the registry
  deploy.sh local --build               build here, reusing cached layers
  deploy.sh local --build --no-cache    build here from scratch
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
    build_args=(--pull)
    cache_note="reusing cached layers"

    if [[ "$NO_CACHE" == true ]]; then
        build_args+=(--no-cache)
        cache_note="from scratch, no cache"
    fi

    # --pull on every build, cache or not. A cached FROM line pins the base
    # image forever, so a Debian or Alpine security update never reaches the
    # gateway no matter how often it is redeployed. That is a slower problem
    # to notice than a stale layer and a worse one to have.
    log "Building images here (${cache_note}; the traffic module compiles from source)"
    compose "$TAG" build "${build_args[@]}" nginx django panel
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
# ---------------------------------------------------------------------------
# A container that fails its healthcheck reports "dependency failed to start"
# and nothing else. The reason is in its log, and the log is one command away
# that nobody runs while reading an error that sounds like a Compose problem.
#
# So it is printed here. The failure that prompted this was Redis coming up
# unhealthy after every capability was dropped from it -- its entrypoint could
# no longer chown its data directory or drop to its own user, and said so in a
# line nobody saw.
# ---------------------------------------------------------------------------
explain_unhealthy() {
    local service
    for service in "$@"; do
        local id
        id=$(compose "$TAG" ps -q "$service" 2>/dev/null | head -1)
        [ -n "$id" ] || continue

        local state
        state=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo unknown)
        [ "$state" = healthy ] && continue

        printf '\n\033[31m%s is %s.\033[0m Its last output:\n\n' "$service" "$state"
        docker logs --tail 30 "$id" 2>&1 | sed 's/^/    /'
        printf '\n'
    done
}

log "Starting the data stores"
if ! compose "$TAG" up -d --wait postgres redis; then
    explain_unhealthy postgres redis
    exit 1
fi

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
