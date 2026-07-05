#!/bin/bash
# Start the relay daemon.
#
# If the systemd service is installed, restarts it.
# Otherwise runs deploy.sh to set everything up.
#
# Usage: bash relay/start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if systemctl --user is-enabled dci-relay &>/dev/null; then
    echo "Restarting dci-relay service..."
    systemctl --user restart dci-relay
    sleep 2
    systemctl --user status dci-relay --no-pager
    echo ""
    echo "Logs: tail -f $PROJECT_DIR/logs/relay.log"
else
    echo "Service not installed. Running deploy.sh..."
    bash "$SCRIPT_DIR/deploy.sh"
fi
