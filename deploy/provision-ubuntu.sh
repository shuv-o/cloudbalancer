#!/usr/bin/env bash
# =============================================================================
# Prepare a fresh Ubuntu server to host the gateway. Run once, as root.
#
#   curl -fsSL https://raw.githubusercontent.com/shuv-o/cloudbalancer/main/deploy/provision-ubuntu.sh | sudo bash
#
# or, having cloned the repository:
#
#   sudo ./deploy/provision-ubuntu.sh
#
# Idempotent: safe to run again after changing something.
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cloudbalancer}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
REPO="${REPO:-https://github.com/shuv-o/cloudbalancer.git}"

[[ $EUID -eq 0 ]] || { echo "Run this as root." >&2; exit 1; }

log() { printf '\n\033[36m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Docker, from Docker's own repository rather than Ubuntu's.
#
# The distribution package lags, and Compose file features this project uses
# need a recent version.
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker"
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
else
    log "Docker already installed: $(docker --version)"
fi

systemctl enable --now docker

compose_version="$(docker compose version --short 2>/dev/null || echo 0)"
log "Compose ${compose_version}"

# ---------------------------------------------------------------------------
# A deploy account that can drive Docker and nothing else.
#
# CI connects as this user. It is in the docker group, which is effectively
# root on this host -- there is no way around that for a Docker deploy, so the
# key that reaches it is as sensitive as a root key and belongs in GitHub
# Actions secrets and nowhere else.
# ---------------------------------------------------------------------------
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    log "Creating the ${DEPLOY_USER} user"
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/${DEPLOY_USER}/.ssh"
touch "/home/${DEPLOY_USER}/.ssh/authorized_keys"
chmod 0600 "/home/${DEPLOY_USER}/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "/home/${DEPLOY_USER}/.ssh/authorized_keys"

# ---------------------------------------------------------------------------
# The application directory.
# ---------------------------------------------------------------------------
if [[ ! -d "$APP_DIR/.git" ]]; then
    log "Cloning into ${APP_DIR}"
    git clone "$REPO" "$APP_DIR"
else
    log "${APP_DIR} already present"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 0600 "$APP_DIR/.env"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"
    log "Created ${APP_DIR}/.env from the example -- edit it before deploying"
fi

# ---------------------------------------------------------------------------
# Firewall.
#
# Docker publishes ports by writing its own iptables rules, which ufw does not
# cover. The real control for the admin surfaces is their bind address in the
# compose file, which is loopback by default. This closes everything else.
# ---------------------------------------------------------------------------
log "Configuring the firewall"
apt-get install -y -qq ufw
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp comment 'ssh' >/dev/null
ufw allow 80/tcp comment 'http and acme challenges' >/dev/null
ufw allow 443/tcp comment 'https' >/dev/null
ufw --force enable >/dev/null
ufw status verbose

# ---------------------------------------------------------------------------
# Host kernel tuning.
#
# The nginx container sets its own sysctls for its namespace; these are the
# host-side equivalents, which matter for the connection backlog the host
# itself accepts.
# ---------------------------------------------------------------------------
log "Tuning the kernel"
cat > /etc/sysctl.d/60-cloudbalancer.conf <<'SYSCTL'
# Gateway host: many short-lived connections, and a SYN flood is answered here
# before any container is involved.
net.core.somaxconn = 8192
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.tcp_syncookies = 1
net.ipv4.ip_local_port_range = 10240 65535
net.ipv4.tcp_fin_timeout = 15
fs.file-max = 2097152
SYSCTL
sysctl -q --system

cat > /etc/security/limits.d/60-cloudbalancer.conf <<'LIMITS'
*  soft  nofile  65536
*  hard  nofile  65536
LIMITS

# ---------------------------------------------------------------------------
# Unattended security updates, and log rotation for the gateway's own logs.
# ---------------------------------------------------------------------------
log "Enabling unattended security upgrades"
apt-get install -y -qq unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

cat > /etc/logrotate.d/cloudbalancer <<'LOGROTATE'
/var/lib/docker/volumes/cloudbalancer_nginx_logs/_data/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
LOGROTATE

cat <<EOF

Done. Next:

  1. Add your CI deploy key to /home/${DEPLOY_USER}/.ssh/authorized_keys
  2. Edit ${APP_DIR}/.env -- every value marked CHANGE ME
  3. Point DNS at this server, then deploy

The admin surfaces bind to loopback. Reach the panel with:

  ssh -L 8081:localhost:8081 ${DEPLOY_USER}@\$(hostname -I | awk '{print \$1}')

EOF
