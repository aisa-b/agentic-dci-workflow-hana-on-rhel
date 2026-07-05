#!/usr/bin/env bash
set -euo pipefail

# Deploy the DCI relay daemon as a containerized systemd service.
#
# Prerequisites:
#   - Podman or Docker installed
#   - SSH access to the jumpbox configured
#   - GCP service account key available
#   - .env file in the repo root with relay secrets
#
# Usage:
#   bash relay/deploy.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RELAY_SH="$PROJECT_DIR/container/relay.sh"

echo "=== DCI Relay — Container Deployment ==="

# Ensure XDG_RUNTIME_DIR is set — required for systemctl --user
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    if [ ! -d "$XDG_RUNTIME_DIR" ]; then
        echo "Creating $XDG_RUNTIME_DIR (requires sudo)..."
        sudo mkdir -p "$XDG_RUNTIME_DIR"
        sudo chown "$(whoami):" "$XDG_RUNTIME_DIR"
        sudo chmod 700 "$XDG_RUNTIME_DIR"
    fi
fi

# --- Step 1: Check container runtime ---
echo ""
echo "--- Checking container runtime ---"
if command -v podman &>/dev/null; then
    RUNTIME="podman"
elif command -v docker &>/dev/null; then
    RUNTIME="docker"
else
    echo "ERROR: Neither podman nor docker found. Install one first."
    exit 1
fi
echo "  Found: $RUNTIME"

# --- Step 2: Verify .env ---
echo ""
echo "--- Checking configuration ---"
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    echo "  Copy .env.example to .env and fill in the relay values."
    exit 1
fi
echo "  Found: $ENV_FILE"

# Check critical variables
source "$ENV_FILE" 2>/dev/null || true
for VAR in GCP_PUBSUB_PROJECT_ID GOOGLE_APPLICATION_CREDENTIALS JUMPBOX_SSH_KEY; do
    VAL="${!VAR:-}"
    if [ -z "$VAL" ]; then
        echo "  WARNING: $VAR is not set in .env"
    else
        echo "  $VAR = $VAL"
    fi
done

# --- Step 3: Verify GCP credentials ---
echo ""
echo "--- Checking GCP credentials ---"
CREDS="${GOOGLE_APPLICATION_CREDENTIALS:-}"
if [ -z "$CREDS" ]; then
    echo "  WARNING: GOOGLE_APPLICATION_CREDENTIALS not set."
elif [ ! -f "$CREDS" ]; then
    echo "  WARNING: Credentials file not found: $CREDS"
else
    echo "  Found: $CREDS"
fi

# --- Step 4: Verify SSH key ---
echo ""
echo "--- Checking SSH key ---"
SSH_KEY="${JUMPBOX_SSH_KEY:-}"
if [ -z "$SSH_KEY" ]; then
    echo "  WARNING: JUMPBOX_SSH_KEY not set."
elif [ ! -f "$SSH_KEY" ]; then
    echo "  WARNING: SSH key not found: $SSH_KEY"
else
    echo "  Found: $SSH_KEY"
fi

# --- Step 5: Build container image ---
echo ""
echo "--- Building container image ---"
bash "$RELAY_SH" build

# --- Step 6: Set up systemd user service ---
echo ""
echo "--- Setting up systemd user service ---"

SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_DIR/dci-relay.service" << EOF
[Unit]
Description=DCI Agent Relay Daemon (containerized)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$RELAY_SH start-fg
ExecStop=$RELAY_SH stop
Restart=always
RestartSec=5
TimeoutStopSec=120

[Install]
WantedBy=default.target
EOF

echo "  Created: $SERVICE_DIR/dci-relay.service"

systemctl --user daemon-reload
echo "  Systemd reloaded"

# --- Step 7: Enable and start ---
echo ""
echo "--- Enabling service ---"

systemctl --user enable dci-relay
echo "  Service enabled (starts on boot)"

# Enable linger so the service survives logout/reboot.
# loginctl can fail with "Failed to connect to bus" on some systems,
# so fall back to creating the linger file directly.
if loginctl enable-linger "$(whoami)" 2>/dev/null; then
    echo "  Linger enabled (via loginctl)"
elif sudo -n mkdir -p /var/lib/systemd/linger && sudo -n touch "/var/lib/systemd/linger/$(whoami)" 2>/dev/null; then
    echo "  Linger enabled (via /var/lib/systemd/linger)"
else
    echo "  WARNING: Could not enable linger. Trying with sudo..."
    sudo mkdir -p /var/lib/systemd/linger
    sudo touch "/var/lib/systemd/linger/$(whoami)"
    echo "  Linger enabled (via sudo)"
fi

# Stop any existing container, then start fresh via systemd
bash "$RELAY_SH" stop 2>/dev/null || true
systemctl --user restart dci-relay
echo "  Service started"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "The relay is running as a containerized systemd service."
echo ""
echo "Commands:"
echo "  systemctl --user status dci-relay              # service status"
echo "  tail -f $PROJECT_DIR/logs/relay.log            # follow logs"
echo "  bash container/relay.sh logs                   # container logs"
echo "  systemctl --user restart dci-relay             # restart"
echo "  systemctl --user stop dci-relay                # stop"
echo "  bash container/relay.sh update                 # git pull + rebuild + restart"
echo ""
echo "To run manually (foreground, for debugging):"
echo "  systemctl --user stop dci-relay"
echo "  bash container/relay.sh start"
