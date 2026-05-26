#!/usr/bin/env bash
# AI Mesh server installer for Debian/Ubuntu with systemd.
# Creates a meshd user, installs deps in a venv, optionally issues a
# Let's Encrypt cert, registers a systemd unit, and starts the service.
#
# Usage (interactive):
#   bash setup-linux.sh
#
# Usage (non-interactive, env vars):
#   DOMAIN=mesh.example.com EMAIL=admin@example.com TLS_MODE=letsencrypt \
#     PORT=443 bash setup-linux.sh
#
# TLS_MODE values:
#   letsencrypt  - issue a real cert via certbot (needs DOMAIN, EMAIL, port 80 free)
#   self-signed  - generate a self-signed cert (browser warning, fine for LAN)
#   provided     - reuse existing CERT_FILE + KEY_FILE paths
#   none         - run plain HTTP (not recommended outside localhost)

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ai-mesh}"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
REPO_URL="${REPO_URL:-https://github.com/Superfish1000/AI-Mesh.git}"
SERVICE_USER="${SERVICE_USER:-meshd}"
PORT="${PORT:-443}"
TLS_MODE="${TLS_MODE:-}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
CERT_FILE="${CERT_FILE:-}"
KEY_FILE="${KEY_FILE:-}"

# Use sudo if available and we're not root
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "error: must run as root or have sudo installed" >&2
        exit 1
    fi
fi

run() { $SUDO "$@"; }

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

prompt() {
    # prompt VAR "Question text" [default]
    local var="$1" question="$2" default="${3:-}"
    if [ -n "${!var:-}" ]; then return; fi
    if [ -n "$default" ]; then
        read -r -p "$question [$default]: " val
        eval "$var=\"\${val:-\$default}\""
    else
        read -r -p "$question: " val
        eval "$var=\"\$val\""
    fi
}

# ── Verify environment ───────────────────────────────────────────────────────
if ! command -v apt-get >/dev/null 2>&1; then
    echo "error: this script targets Debian/Ubuntu (apt-get not found)" >&2
    exit 1
fi
if ! ps -p 1 -o comm= | grep -q systemd; then
    echo "error: systemd is not PID 1 — this script needs systemd" >&2
    echo "       (try setup-linux-nohup.sh or a docker-compose deployment)" >&2
    exit 1
fi

# ── Collect config ───────────────────────────────────────────────────────────
if [ -z "$TLS_MODE" ]; then
    echo "TLS mode:"
    echo "  1) letsencrypt  - real cert (needs public domain + port 80)"
    echo "  2) self-signed  - browser warning, fine for LAN/VPN"
    echo "  3) provided     - bring your own cert"
    echo "  4) none         - plain HTTP"
    read -r -p "Choice [1-4]: " choice
    case "$choice" in
        1) TLS_MODE="letsencrypt" ;;
        2) TLS_MODE="self-signed" ;;
        3) TLS_MODE="provided" ;;
        4) TLS_MODE="none" ;;
        *) echo "invalid choice"; exit 1 ;;
    esac
fi

case "$TLS_MODE" in
    letsencrypt)
        prompt DOMAIN "Domain name (e.g. mesh.example.com)"
        prompt EMAIL  "Email for Let's Encrypt notifications"
        [ -z "$DOMAIN" ] && { echo "DOMAIN is required"; exit 1; }
        [ -z "$EMAIL" ]  && { echo "EMAIL is required"; exit 1; }
        ;;
    provided)
        prompt CERT_FILE "Full path to existing cert.pem"
        prompt KEY_FILE  "Full path to existing key.pem"
        [ -f "$CERT_FILE" ] || { echo "cert not found: $CERT_FILE"; exit 1; }
        [ -f "$KEY_FILE" ]  || { echo "key not found: $KEY_FILE";  exit 1; }
        ;;
esac

# ── Install system packages ──────────────────────────────────────────────────
log "Installing system packages"
run apt-get update
PKGS="python3 python3-venv python3-pip git"
[ "$TLS_MODE" = "letsencrypt" ] && PKGS="$PKGS certbot"
run apt-get install -y $PKGS

# ── Create service user ──────────────────────────────────────────────────────
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating user '$SERVICE_USER'"
    run useradd --system --create-home --home-dir "$INSTALL_DIR" \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ── Clone or update repo ─────────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    log "Updating repo at $APP_DIR"
    run -u "$SERVICE_USER" git -C "$APP_DIR" pull --ff-only
else
    log "Cloning repo to $APP_DIR"
    run install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
    run -u "$SERVICE_USER" git clone "$REPO_URL" "$APP_DIR"
fi

# ── Python venv + deps ───────────────────────────────────────────────────────
log "Setting up Python venv"
if [ ! -d "$VENV_DIR" ]; then
    run -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
fi
run -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip
run -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/server/requirements.txt"

# ── TLS material ─────────────────────────────────────────────────────────────
SSL_ARGS=""
case "$TLS_MODE" in
    letsencrypt)
        log "Requesting Let's Encrypt cert for $DOMAIN"
        run certbot certonly --standalone --non-interactive --agree-tos \
            --email "$EMAIL" -d "$DOMAIN"
        # Let meshd read the cert files
        run chgrp -R "$SERVICE_USER" /etc/letsencrypt/live /etc/letsencrypt/archive
        run chmod -R g+rX /etc/letsencrypt/live /etc/letsencrypt/archive
        CERT_FILE="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
        KEY_FILE="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
        SSL_ARGS="--ssl-certfile $CERT_FILE --ssl-keyfile $KEY_FILE"

        log "Installing renewal deploy hook"
        run mkdir -p /etc/letsencrypt/renewal-hooks/deploy
        run tee /etc/letsencrypt/renewal-hooks/deploy/ai-mesh-restart.sh >/dev/null <<'HOOK'
#!/bin/sh
systemctl restart ai-mesh
HOOK
        run chmod +x /etc/letsencrypt/renewal-hooks/deploy/ai-mesh-restart.sh
        ;;
    self-signed)
        log "Generating self-signed cert"
        run -u "$SERVICE_USER" "$VENV_DIR/bin/python" "$APP_DIR/server/gen_cert.py"
        CERT_FILE="$APP_DIR/server/cert.pem"
        KEY_FILE="$APP_DIR/server/key.pem"
        SSL_ARGS="--ssl-certfile $CERT_FILE --ssl-keyfile $KEY_FILE"
        ;;
    provided)
        run install -m 644 -o "$SERVICE_USER" -g "$SERVICE_USER" \
            "$CERT_FILE" "$APP_DIR/server/cert.pem"
        run install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
            "$KEY_FILE"  "$APP_DIR/server/key.pem"
        SSL_ARGS="--ssl-certfile $APP_DIR/server/cert.pem --ssl-keyfile $APP_DIR/server/key.pem"
        ;;
    none)
        SSL_ARGS=""
        ;;
esac

# ── systemd unit ─────────────────────────────────────────────────────────────
log "Writing systemd unit"
UNIT=/etc/systemd/system/ai-mesh.service
run tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=AI Mesh coordination server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR/server
Environment=PATH=$VENV_DIR/bin
ExecStart=$VENV_DIR/bin/uvicorn server:app --host 0.0.0.0 --port $PORT $SSL_ARGS
Restart=on-failure
RestartSec=5

# Bind to privileged ports (<1024) without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/server

[Install]
WantedBy=multi-user.target
EOF

log "Enabling and starting ai-mesh.service"
run systemctl daemon-reload
run systemctl enable --now ai-mesh.service

# ── Show bootstrap URL ───────────────────────────────────────────────────────
sleep 2
log "Recent server log (look for the /setup?token=... URL):"
run journalctl -u ai-mesh --no-pager -n 30 || true

cat <<MSG

═══════════════════════════════════════════════════════════════════
 AI Mesh is running.

 Service control:
   systemctl status ai-mesh
   journalctl -u ai-mesh -f
   systemctl restart ai-mesh

 If this is the first run, the setup URL is in the log above.
 Open it in a browser to create your admin account.
═══════════════════════════════════════════════════════════════════
MSG
