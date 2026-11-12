#!/usr/bin/env bash
# =============================================================================
# Report everything needed to diagnose a gateway that is not working.
#
#   cd /opt/cloudbalancer && ./deploy/doctor.sh
#
# Prints container states, the startup failure if there is one, what Django
# thinks its settings are, and whether each hop in the chain actually connects.
#
# Secrets are never printed. Values from .env are shown as set/unset and, for
# the ones that affect reachability, their actual value -- those are hostnames
# and booleans, not credentials.
# =============================================================================
set -uo pipefail

cd "${APP_DIR:-$(dirname "$(readlink -f "$0")")/..}" || exit 1

hr() { printf '\n\033[36m── %s %s\033[0m\n' "$1" "$(printf '─%.0s' $(seq 1 $((60 - ${#1}))))"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
no() { printf '  \033[31m✗\033[0m %s\n' "$1"; }

hr "Versions"
printf '  docker   %s\n' "$(docker --version 2>&1 | cut -d, -f1)"
printf '  compose  %s\n' "$(docker compose version --short 2>&1)"
printf '  commit   %s\n' "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '  deployed %s\n' "$(cat .deployed-tag 2>/dev/null || echo 'none recorded')"

hr "Containers"
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}' 2>&1 | sed 's/^/  /'

hr "Restart counts"
# A container restarting repeatedly is failing at startup, which the status
# column alone does not make obvious.
for c in $(docker compose ps -aq 2>/dev/null); do
    name=$(docker inspect -f '{{.Name}}' "$c" | sed 's|^/||')
    count=$(docker inspect -f '{{.RestartCount}}' "$c")
    code=$(docker inspect -f '{{.State.ExitCode}}' "$c")
    state=$(docker inspect -f '{{.State.Status}}' "$c")
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c")
    printf '  %-32s %-10s restarts=%-4s exit=%-4s health=%s\n' \
        "$name" "$state" "$count" "$code" "$health"
done

hr "Why Django stopped, if it did"
# The startup chain is migrate && collectstatic && bootstrap && gunicorn. A
# failure in any of them exits the container, and the reason is the last
# thing before it went.
#
# With timestamps, because this is the whole log rather than the last attempt.
# A container that has been restarting for an hour still contains every
# failure from before whatever fixed it, and reading the oldest one as
# current has cost more than one round trip.
docker compose logs --tail 40 --no-log-prefix --timestamps django 2>&1 \
    | grep -viE 'DisallowedHost|Traceback|^\S+ +File "|^\S+ +[a-z_]+ = |^\S+ +\^+$|^\S+ +response|^\S+ +host|^\S+ +raise' \
    | tail -25 | sed 's/^/  /'

printf '\n  \033[2m(timestamps above: anything older than your last\033[0m\n'
printf '  \033[2m run of make fix-permissions has already been dealt with)\033[0m\n'

hr "Settings Django actually loaded"
docker compose exec -T django python -c "
from django.conf import settings
import django, os
django.setup()
print('  ALLOWED_HOSTS        ', settings.ALLOWED_HOSTS)
print('  CSRF_TRUSTED_ORIGINS ', settings.CSRF_TRUSTED_ORIGINS)
print('  DEBUG                ', settings.DEBUG)
print('  PANEL_COOKIE_SECURE  ', getattr(settings, 'PANEL_COOKIE_SECURE', 'NOT IN THIS BUILD'))
print('  SESSION_COOKIE_SECURE', settings.SESSION_COOKIE_SECURE)
print('  SECRET_KEY set       ', bool(settings.SECRET_KEY) and settings.SECRET_KEY != 'CHANGE_ME')
" 2>&1 | sed 's/^/  /' | grep -v "^  $"

hr "Can each hop connect?"
if docker compose exec -T django python -c "
import socket,sys
try:
    socket.create_connection(('postgres',5432),timeout=5).close()
except Exception as e:
    sys.exit(str(e))" 2>/dev/null; then ok "django reaches postgres"; else no "django cannot reach postgres"; fi

if docker compose exec -T django python -c "
import socket,sys
try:
    socket.create_connection(('redis',6379),timeout=5).close()
except Exception as e:
    sys.exit(str(e))" 2>/dev/null; then ok "django reaches redis"; else no "django cannot reach redis"; fi

if docker compose exec -T nginx wget -qO- --timeout=5 http://django:8000/api/v1/auth/me/ >/dev/null 2>&1; then
    ok "nginx reaches django"
else
    no "nginx cannot reach django  <-- this is what makes the panel answer 503"
fi

# The output, not just the verdict. "nginx config invalid" is a fact nobody
# can act on, and the reason is one line that this was discarding.
if nginx_test=$(docker compose exec -T nginx nginx -t 2>&1); then
    ok "nginx config valid"
else
    no "nginx config invalid"
    printf '%s\n' "$nginx_test" | sed 's/^/      /'
    if printf '%s' "$nginx_test" | grep -q 'host not found in upstream'; then
        printf '\n      \033[33mA name in an upstream block did not resolve. If it is a\033[0m\n'
        printf '      \033[33mbackend, check it is reachable from this host. If it is\033[0m\n'
        printf '      \033[33mdjango, the generated config predates the change that\033[0m\n'
        printf '      \033[33mstopped it being an upstream at all:  make deploy\033[0m\n'
    fi
fi

hr "Database"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-cloudbalancer}" \
    -d "${POSTGRES_DB:-cloudbalancer}" -c '\dt' 2>&1 | tail -5 | sed 's/^/  /'

hr "From outside"
printf '  panel      %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8081/ || echo unreachable)"
printf '  panel api  %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8081/api/v1/auth/me/ || echo unreachable)"
printf '  gateway    %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost/health || echo unreachable)"

hr "Is the live config what the database would generate?"

# The outage this exists for: a change to the hand-written config removed an
# upstream that the generated files still referenced, the control plane was
# down and could not regenerate them, and Nginx refused to start on a file
# nobody knew was stale. Nothing reported the drift, because nothing looked.
if docker compose exec -T django python manage.py deploy_config \
       --render-to /tmp/cb-drift >/dev/null 2>&1; then
    drift=0
    for name in 00-maps.conf 05-panel.conf 10-upstreams.conf 20-servers.conf; do
        live="gateway/nginx/conf.d/${name}"
        [[ -f "$live" ]] || continue
        if ! docker compose exec -T django \
               cmp -s "/tmp/cb-drift/${name}" "/etc/nginx/conf.d/${name}" 2>/dev/null; then
            no "${name} on disk differs from what the database would generate"
            drift=1
        fi
    done
    if [[ $drift -eq 0 ]]; then
        ok "every generated file matches the database"
    else
        printf '\n      \033[33mSomething changed without a deploy. If the gateway is\033[0m\n'
        printf '      \033[33mrunning:  make deploy\033[0m\n'
        printf '      \033[33mIf it is refusing to start:  make repair-config\033[0m\n'
    fi
    docker compose exec -T django rm -rf /tmp/cb-drift >/dev/null 2>&1 || true
else
    no "could not render a comparison -- is the control plane running?"
fi

hr "Ownership the management plane needs"

# Asked here because the symptom -- a PermissionError deep in collectstatic,
# or a container restarting -- is several steps from the cause, and the logs
# that show it are historical: after running `make fix-permissions` they still
# contain every failure from before it.
for path in management/staticfiles gateway/nginx/conf.d; do
    if [[ ! -e "$path" ]]; then
        no "$path does not exist"
        continue
    fi
    owner=$(stat -c '%u' "$path" 2>/dev/null || stat -f '%u' "$path" 2>/dev/null)
    if [[ "$owner" == "10001" ]]; then
        ok "$path belongs to the management plane"
    else
        no "$path belongs to uid ${owner:-?}, not 10001 -- run: make fix-permissions"
    fi
done

for volume in letsencrypt ssl_certs acme_challenge nginx_control; do
    name=$(docker volume ls -q | grep -E "_${volume}$" | head -1)
    [[ -n "$name" ]] || continue
    mount=$(docker volume inspect -f '{{.Mountpoint}}' "$name" 2>/dev/null) || continue
    [[ -d "$mount" ]] || continue
    owner=$(stat -c '%u' "$mount" 2>/dev/null)
    if [[ "$owner" == "10001" ]]; then
        ok "volume ${volume} belongs to the management plane"
    else
        no "volume ${volume} belongs to uid ${owner:-?}, not 10001 -- run: make fix-permissions"
    fi
done

hr "Exposure"
# `docker compose ps` lists published ports as a fact. It does not say whether
# an admin surface ended up on a public interface -- and that is the one
# mistake here that cannot be taken back once somebody has found it. So this
# section judges rather than prints.
published=$(docker compose ps --format '{{.Service}}|{{.Ports}}' 2>/dev/null)
public_admin=0

check_surface() {
    local service=$1 port=$2 label=$3 line
    line=$(printf '%s\n' "$published" | grep "^${service}|")

    if printf '%s' "$line" | grep -qE "(0\.0\.0\.0|\[::\]):${port}->"; then
        no "${label} is published on every interface (:${port})"
        public_admin=1
    elif printf '%s' "$line" | grep -qE ":${port}->"; then
        ok "${label} is bound to one address (:${port})"
    else
        ok "${label} is not published (:${port})"
    fi
}

check_surface nginx      8081 "control panel"
check_surface grafana    3000 "Grafana"
check_surface prometheus 9090 "Prometheus"

# ---------------------------------------------------------------------------
# Container posture
#
# These used to be invisible. The management containers mounted the Docker
# socket, which is the Docker API with no authorisation in front of it: a
# compromise anywhere in the panel was root on this host rather than a
# compromised web application.
# ---------------------------------------------------------------------------
for service in django celery celery-beat; do
    cid=$(docker compose ps -q "$service" 2>/dev/null | head -1)
    [[ -n "$cid" ]] || continue

    if docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' "$cid" 2>/dev/null \
        | grep -q docker.sock; then
        no "$service still mounts the Docker socket, which is root on this host"
    else
        ok "$service cannot reach the Docker API"
    fi

    runs_as=$(docker inspect -f '{{.Config.User}}' "$cid" 2>/dev/null)
    if [[ -z "$runs_as" || "$runs_as" == "root" || "$runs_as" == 0* ]]; then
        no "$service runs as root"
    else
        ok "$service runs as $runs_as"
    fi

    if docker inspect -f '{{range .HostConfig.SecurityOpt}}{{.}} {{end}}' "$cid" 2>/dev/null \
        | grep -q no-new-privileges; then
        ok "$service cannot gain privileges"
    else
        no "$service may gain privileges through setuid binaries"
    fi
done

if [[ $public_admin -eq 1 ]]; then
    # A session cookie without Secure travels over any plain-HTTP request to
    # the same host. On a loopback-only panel that is contained. On a public
    # one it is the entire authentication story crossing the network in clear.
    cookie=$(docker compose exec -T django python -c "
import django; django.setup()
from django.conf import settings
print(settings.SESSION_COOKIE_SECURE)" 2>/dev/null | tr -d '\r' | tail -1)
    [[ "$cookie" == "False" ]] && \
        no "the session cookie is not marked Secure, so it crosses that interface in clear text"

    printf '\n  \033[31mAn admin surface answers anyone who can route to this host.\033[0m\n'
    printf '  Close it   set PANEL_BIND=127.0.0.1 and OBSERVABILITY_BIND=127.0.0.1 in .env,\n'
    printf '             then: docker compose up -d\n'
    printf '  Reach it   ssh -L 8081:localhost:8081 <user>@<this host>, then http://localhost:8081\n'
    printf '  Or keep remote access properly: give the panel a hostname and a\n'
    printf '  certificate under Security -> Publish the panel, which serves it over\n'
    printf '  TLS on 443 with HSTS, a CSP, rate limits and an optional allowlist.\n'
fi

hr "Published ports"
docker compose ps --format '{{.Service}} {{.Ports}}' 2>/dev/null | grep -E '0.0.0.0|127.0.0.1|:::' | sed 's/^/  /'

printf '\n\033[36mPaste this whole output when asking for help. It contains no secrets.\033[0m\n\n'
