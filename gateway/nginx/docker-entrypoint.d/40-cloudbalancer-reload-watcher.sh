#!/bin/sh
#
# Starts the reload watcher before Nginx takes over as PID 1.
#
# The stock image runs everything in /docker-entrypoint.d in order and then
# execs Nginx, so this must return rather than block -- hence the background
# job. It outlives this script because the entrypoint does not reap it, and
# dies with the container because PID 1 does.
#
set -e

/usr/local/bin/cloudbalancer-reload-watcher.sh &

echo "[cloudbalancer] reload watcher started (pid $!)"
