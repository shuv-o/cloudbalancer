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
docker compose logs --tail 40 --no-log-prefix django 2>&1 \
    | grep -viE 'DisallowedHost|Traceback|^\s+File "|^\s+[a-z_]+ = |^\s+\^+$|^\s+response|^\s+host|^\s+raise' \
    | tail -25 | sed 's/^/  /'

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

if docker compose exec -T nginx nginx -t >/dev/null 2>&1; then ok "nginx config valid"; else no "nginx config invalid"; fi

hr "Database"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-cloudbalancer}" \
    -d "${POSTGRES_DB:-cloudbalancer}" -c '\dt' 2>&1 | tail -5 | sed 's/^/  /'

hr "From outside"
printf '  panel      %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8081/ || echo unreachable)"
printf '  panel api  %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8081/api/v1/auth/me/ || echo unreachable)"
printf '  gateway    %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost/health || echo unreachable)"

hr "Published ports"
docker compose ps --format '{{.Service}} {{.Ports}}' 2>/dev/null | grep -E '0.0.0.0|127.0.0.1|:::' | sed 's/^/  /'

printf '\n\033[36mPaste this whole output when asking for help. It contains no secrets.\033[0m\n\n'
