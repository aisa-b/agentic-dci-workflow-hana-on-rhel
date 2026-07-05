#!/usr/bin/env bash
#
# Start the DCI multi-agent container on the relay machine.
#
# Usage:
#   ./container/run.sh
#
# Prerequisites:
#   - Podman installed on the relay machine
#   - .env file in the repo root with all required variables
#   - SSH key for target server access
#   - gh CLI authenticated (gh auth login)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Source env vars
if [[ -f "$REPO_DIR/.env" ]]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
else
    echo "ERROR: .env file not found in $REPO_DIR"
    echo "Copy .env.example to .env and fill in the values."
    exit 1
fi

IMAGE_NAME="dci-agent"
CONTAINER_NAME="dci-agent-run"

# Build the image
echo "Building container image..."
podman build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Containerfile" "$REPO_DIR"

# Pull latest code in the hooks repo
echo "Pulling latest code in $DCI_REPO_ROOT..."
git -C "$DCI_REPO_ROOT" pull --ff-only || echo "WARNING: git pull failed, using current state"

# Run the container
echo "Starting agent container..."
podman run --rm \
    --name "$CONTAINER_NAME" \
    --network host \
    --env-file "$REPO_DIR/.env" \
    -v "$DCI_REPO_ROOT:$DCI_REPO_ROOT:rw" \
    -v "${DCI_TARGET_SSH_KEY}:${DCI_TARGET_SSH_KEY}:ro" \
    -v "${HOME}/.gitconfig:/root/.gitconfig:ro" \
    "$IMAGE_NAME"

echo "Agent run complete."
