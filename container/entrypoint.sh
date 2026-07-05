#!/bin/bash
# No set -e — we handle errors explicitly to avoid silent exits.

# ---- Pre-flight checks ----
preflight_ok=true

if [[ ! -f /secrets/ssh-key ]]; then
    echo "WARNING: SSH key not mounted at /secrets/ssh-key -- jumpbox commands will fail"
    preflight_ok=false
else
    mkdir -p /tmp/.ssh
    cp /secrets/ssh-key /tmp/.ssh/id_key
    chmod 600 /tmp/.ssh/id_key
    export JUMPBOX_SSH_KEY=/tmp/.ssh/id_key
fi

if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" && ! -f /secrets/gcp-sa-key.json ]]; then
    echo "WARNING: GCP SA key not found -- Pub/Sub will fail"
    echo "  Mount: -v /path/to/key.json:/secrets/gcp-sa-key.json:ro"
    echo "  Or set: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json"
    preflight_ok=false
elif [[ -f /secrets/gcp-sa-key.json && -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
    export GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa-key.json
fi

if [[ -z "${GCP_PUBSUB_PROJECT_ID:-}" ]]; then
    echo "WARNING: GCP_PUBSUB_PROJECT_ID not set -- Pub/Sub will fail"
    preflight_ok=false
fi

# If the host repo is mounted, use it as the working directory
if [[ -d /repo/.git ]]; then
    export REPO_ROOT=/repo
    cd /repo
    git config --global --add safe.directory /repo
    if [[ -f /secrets/git-credentials ]]; then
        cp /secrets/git-credentials /tmp/.git-credentials
        git config --global credential.helper 'store --file=/tmp/.git-credentials'
    fi
    git pull --ff-only 2>&1 || echo "WARNING: git pull failed, continuing with current code"
else
    echo "WARNING: Host repo not mounted at /repo -- using baked-in code"
fi

if [[ ! -f "${REPO_ROOT:-/opt/app-root/src}/run_config.yml" ]]; then
    echo "WARNING: run_config.yml not found -- workflow commands will fail"
    preflight_ok=false
fi

if [[ "$preflight_ok" == "false" ]]; then
    echo ""
    echo "Pre-flight warnings detected. The daemon will start but some operations may fail."
    echo "Fix the warnings above and restart the container."
    echo ""
fi

# Log directory setup
LOG_DIR="${REPO_ROOT:-/opt/app-root/src}/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/relay.log"

echo "--- Daemon starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
echo "--- Daemon starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ---" >> "$LOG_FILE" 2>/dev/null || true
echo "Python: $(python --version 2>&1)"
echo "CWD: $(pwd)"
echo "PYTHONPATH: ${PYTHONPATH:-not set}"
echo "User: $(id)"

# Run the daemon directly — output goes to stdout (podman logs captures it)
# and also appends to the log file via tee. If tee fails, ignore it.
exec python -u -m relay.daemon 2>&1
