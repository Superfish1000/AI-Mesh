#!/usr/bin/env bash
# AI Mesh updater for Linux installs created with setup-linux.sh.
# Pulls the latest code, refreshes Python deps, and restarts the service.
#
# Usage:
#   bash update-linux.sh

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ai-mesh}"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_USER="${SERVICE_USER:-meshd}"
SERVICE_NAME="${SERVICE_NAME:-ai-mesh}"

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

run_as() {
    local user="$1"; shift
    if [ -z "$SUDO" ]; then
        su -s /bin/bash "$user" -c "$(printf '%q ' "$@")"
    else
        sudo -u "$user" "$@"
    fi
}

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

[ -d "$APP_DIR/.git" ] || { echo "error: $APP_DIR is not a git repo (run setup-linux.sh first)"; exit 1; }
[ -x "$VENV_DIR/bin/pip" ] || { echo "error: venv missing at $VENV_DIR (run setup-linux.sh first)"; exit 1; }

log "Pulling latest code"
run_as "$SERVICE_USER" git -C "$APP_DIR" pull --ff-only

log "Refreshing Python dependencies"
run_as "$SERVICE_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/server/requirements.txt"

log "Restarting $SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"

sleep 1
$SUDO systemctl status "$SERVICE_NAME" --no-pager --lines=10 || true

log "Done."
