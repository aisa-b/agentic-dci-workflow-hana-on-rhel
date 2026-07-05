#!/usr/bin/env bash
#
# DCI Relay — container management script
#
# Usage:
#   ./relay.sh build     Build the container image
#   ./relay.sh start     Start the relay daemon (auto-tails logs)
#   ./relay.sh stop      Stop the relay daemon
#   ./relay.sh restart   Stop + start
#   ./relay.sh update    git pull + rebuild + restart
#   ./relay.sh logs      Follow container logs
#   ./relay.sh status    Show container status
#   ./relay.sh shell     Open a shell inside the running container
#
# Prerequisites:
#   - Podman or Docker installed
#   - .env file in the repo root with relay secrets
#   - GCP service account key file
#   - SSH key for jumpbox access
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="dci-relay"
CONTAINER_NAME="dci-relay"

if ! command -v podman &>/dev/null && ! command -v docker &>/dev/null; then
    echo "No container runtime found. Installing podman..."
    sudo dnf install -y podman 2>/dev/null || sudo yum install -y podman
fi

if command -v podman &>/dev/null; then
    RUNTIME="podman"
elif command -v docker &>/dev/null; then
    RUNTIME="docker"
else
    echo "ERROR: Failed to install podman. Install manually: sudo dnf install -y podman"
    exit 1
fi

echo "Using container runtime: $RUNTIME"

# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------
_parse_env_val() {
    local line
    line=$(grep -E "^$1=" "$2" 2>/dev/null | head -1) || true
    line="${line#*=}"
    line="${line#\"}"
    line="${line%\"}"
    line="${line#\'}"
    line="${line%\'}"
    echo "$line"
}

_ensure_env() {
    local env_file="$REPO_ROOT/.env"
    if [[ ! -f "$env_file" ]]; then
        echo "No .env found. Creating template..."
        cat > "$env_file" << 'ENVEOF'
GCP_PUBSUB_PROJECT_ID=your-pubsub-project-id
GOOGLE_APPLICATION_CREDENTIALS=/path/to/dci-relay-sa-key.json
JUMPBOX_SSH_KEY=/path/to/.ssh/id_ed25519
ENVEOF
        echo ""
        echo "Created $env_file with placeholder values."
        echo "Edit it now and replace the paths, then re-run this command."
        echo ""
        echo "  GOOGLE_APPLICATION_CREDENTIALS  = path to GCP service account key (JSON)"
        echo "  JUMPBOX_SSH_KEY                 = path to SSH private key for the jumpbox"
        echo "  GCP_PUBSUB_PROJECT_ID           = GCP project for Pub/Sub"
        echo ""
        exit 1
    fi
}

_read_env() {
    _ensure_env

    local env_file="$REPO_ROOT/.env"

    GOOGLE_APPLICATION_CREDENTIALS=$(_parse_env_val GOOGLE_APPLICATION_CREDENTIALS "$env_file")
    JUMPBOX_SSH_KEY=$(_parse_env_val JUMPBOX_SSH_KEY "$env_file")
    GCP_PUBSUB_PROJECT_ID=$(_parse_env_val GCP_PUBSUB_PROJECT_ID "$env_file")
    GIT_CREDENTIALS_FILE=$(_parse_env_val GIT_CREDENTIALS_FILE "$env_file")
    GIT_CREDENTIALS_FILE="${GIT_CREDENTIALS_FILE:-$HOME/.git-credentials}"

    local errors=0
    if [[ -z "$GOOGLE_APPLICATION_CREDENTIALS" ]]; then
        echo "ERROR: GOOGLE_APPLICATION_CREDENTIALS not set in .env"; errors=1
    elif [[ ! -f "$GOOGLE_APPLICATION_CREDENTIALS" ]]; then
        echo "ERROR: GCP SA key not found: $GOOGLE_APPLICATION_CREDENTIALS"; errors=1
    fi
    if [[ -z "$JUMPBOX_SSH_KEY" ]]; then
        echo "ERROR: JUMPBOX_SSH_KEY not set in .env"; errors=1
    elif [[ ! -f "$JUMPBOX_SSH_KEY" ]]; then
        echo "ERROR: SSH key not found: $JUMPBOX_SSH_KEY"; errors=1
    fi
    if [[ -z "$GCP_PUBSUB_PROJECT_ID" ]]; then
        echo "ERROR: GCP_PUBSUB_PROJECT_ID not set in .env"; errors=1
    fi

    if [[ "$errors" -eq 1 ]]; then
        echo ""
        echo "Fix the values in $env_file and re-run."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
cmd_clean() {
    echo "=== Cleaning up previous relay state ==="

    # 1. Stop and remove named container
    if $RUNTIME ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        echo "Stopping container $CONTAINER_NAME..."
        $RUNTIME stop -t 5 "$CONTAINER_NAME" 2>/dev/null || true
        $RUNTIME rm -f "$CONTAINER_NAME" 2>/dev/null || true
    fi

    # 2. Kill any hanging podman/conmon processes for dci-relay
    local pids
    pids=$(pgrep -f "podman.*${CONTAINER_NAME}\|conmon.*${CONTAINER_NAME}" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "Killing hanging processes: $pids"
        echo "$pids" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi

    # 3. Remove any orphan containers using our image
    local orphans
    orphans=$($RUNTIME ps -a --filter "ancestor=$IMAGE_NAME" --format '{{.ID}}' 2>/dev/null || true)
    if [[ -n "$orphans" ]]; then
        echo "Removing orphan containers..."
        echo "$orphans" | xargs $RUNTIME rm -f 2>/dev/null || true
    fi

    # 4. Clean up stale podman storage state
    if $RUNTIME ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        $RUNTIME rm -f "$CONTAINER_NAME" 2>/dev/null || true
    fi

    # 5. Fix log file ownership if needed
    if [[ -d "$REPO_ROOT/logs" ]]; then
        local log_owner
        log_owner=$(stat -c '%u' "$REPO_ROOT/logs" 2>/dev/null || echo "0")
        if [[ "$log_owner" != "$(id -u)" ]]; then
            echo "Fixing log directory ownership (was UID $log_owner)..."
            # Try without sudo first, fall back to warning
            chown -R "$(id -u)" "$REPO_ROOT/logs" 2>/dev/null || \
                echo "  WARNING: Cannot fix ownership. Run: sudo chown -R $(id -u) $REPO_ROOT/logs"
        fi
    fi

    # 6. Fix audit.jsonl ownership
    if [[ -f "$REPO_ROOT/audit.jsonl" ]]; then
        local audit_owner
        audit_owner=$(stat -c '%u' "$REPO_ROOT/audit.jsonl" 2>/dev/null || echo "0")
        if [[ "$audit_owner" != "$(id -u)" ]]; then
            chown "$(id -u)" "$REPO_ROOT/audit.jsonl" 2>/dev/null || \
                echo "  WARNING: Cannot fix audit.jsonl ownership. Run: sudo chown $(id -u) $REPO_ROOT/audit.jsonl"
        fi
    fi

    echo "Cleanup complete."
}

cmd_preflight() {
    echo "=== DCI Relay Pre-flight Check ==="
    echo ""
    local errors=0

    # 1. Container runtime
    echo "[1/10] Container runtime..."
    if command -v "$RUNTIME" &>/dev/null; then
        echo "  OK: $RUNTIME ($($RUNTIME --version 2>/dev/null | head -1))"
    else
        echo "  FAIL: $RUNTIME not found"; errors=$((errors+1))
    fi

    # 2. .env file
    echo "[2/10] .env file..."
    local env_file="$REPO_ROOT/.env"
    if [[ -f "$env_file" ]]; then
        echo "  OK: $env_file exists"
        # Check required keys
        for key in GCP_PUBSUB_PROJECT_ID GOOGLE_APPLICATION_CREDENTIALS JUMPBOX_SSH_KEY; do
            local val=$(_parse_env_val "$key" "$env_file")
            if [[ -z "$val" ]]; then
                echo "  FAIL: $key not set in .env"; errors=$((errors+1))
            else
                echo "  OK: $key = ${val:0:40}..."
            fi
        done
    else
        echo "  FAIL: $env_file not found"; errors=$((errors+1))
    fi

    # 3. GCP SA key file
    echo "[3/10] GCP service account key..."
    local sa_key=$(_parse_env_val "GOOGLE_APPLICATION_CREDENTIALS" "$env_file" 2>/dev/null)
    if [[ -n "$sa_key" && -f "$sa_key" ]]; then
        echo "  OK: $sa_key exists ($(stat -c %s "$sa_key" 2>/dev/null || echo '?') bytes)"
        echo "  Owner: $(stat -c '%U:%G' "$sa_key" 2>/dev/null || echo '?')"
    elif [[ -n "$sa_key" ]]; then
        echo "  FAIL: $sa_key does not exist"; errors=$((errors+1))
    else
        echo "  SKIP: GOOGLE_APPLICATION_CREDENTIALS not set"
    fi

    # 4. SSH key file
    echo "[4/10] SSH key..."
    local ssh_key=$(_parse_env_val "JUMPBOX_SSH_KEY" "$env_file" 2>/dev/null)
    if [[ -n "$ssh_key" && -f "$ssh_key" ]]; then
        local perms
        perms=$(stat -c %a "$ssh_key" 2>/dev/null)
        echo "  OK: $ssh_key exists (permissions: $perms)"
        if [[ "$perms" != "600" && "$perms" != "400" ]]; then
            echo "  WARN: SSH key permissions should be 600 or 400, got $perms"
        fi
    elif [[ -n "$ssh_key" ]]; then
        echo "  FAIL: $ssh_key does not exist"; errors=$((errors+1))
    else
        echo "  SKIP: JUMPBOX_SSH_KEY not set"
    fi

    # 5. run_config.yml
    echo "[5/10] run_config.yml..."
    if [[ -f "$REPO_ROOT/run_config.yml" ]]; then
        echo "  OK: $REPO_ROOT/run_config.yml exists"
    else
        echo "  WARN: run_config.yml not found (relay will fall back to .env only)"
    fi

    # 6. Log directory
    echo "[6/10] Log directory..."
    local log_dir="$REPO_ROOT/logs"
    mkdir -p "$log_dir" 2>/dev/null
    if [[ -d "$log_dir" ]]; then
        local log_owner
        log_owner=$(stat -c '%u' "$log_dir" 2>/dev/null)
        echo "  OK: $log_dir exists (owner UID: $log_owner, your UID: $(id -u))"
        if [[ "$log_owner" != "$(id -u)" ]]; then
            echo "  WARN: Log dir owned by UID $log_owner, not you ($(id -u)). Fix: sudo chown -R $(id -u) $log_dir"
        fi
        # Test write
        if touch "$log_dir/.preflight_test" 2>/dev/null; then
            rm -f "$log_dir/.preflight_test"
            echo "  OK: Log directory is writable"
        else
            echo "  FAIL: Cannot write to $log_dir"; errors=$((errors+1))
        fi
    else
        echo "  FAIL: Cannot create $log_dir"; errors=$((errors+1))
    fi

    # 7. Container image
    echo "[7/10] Container image..."
    if $RUNTIME image exists "$IMAGE_NAME" 2>/dev/null; then
        local img_created
        img_created=$($RUNTIME image inspect "$IMAGE_NAME" --format '{{.Created}}' 2>/dev/null | cut -d'.' -f1)
        echo "  OK: $IMAGE_NAME exists (created: $img_created)"
    else
        echo "  WARN: Image $IMAGE_NAME not built yet. Run: bash $0 build"
    fi

    # 8. Podman storage
    echo "[8/10] Podman storage..."
    local graphroot
    graphroot=$($RUNTIME info --format '{{.Store.GraphRoot}}' 2>/dev/null)
    echo "  GraphRoot: $graphroot"
    if [[ "$graphroot" == /sapmnt/* || "$graphroot" == /home/* ]]; then
        local fs_type
        fs_type=$(df -T "$graphroot" 2>/dev/null | tail -1 | awk '{print $2}')
        if [[ "$fs_type" == "nfs"* ]]; then
            echo "  WARN: Podman storage is on NFS ($fs_type). This causes UID mapping issues."
            echo "        Fix: Move to local storage (see GETTING_STARTED.md)"
        else
            echo "  OK: Filesystem type: $fs_type"
        fi
    else
        echo "  OK: Storage on local path"
    fi

    # 9. DNS resolution
    echo "[9/10] DNS resolution..."
    if getent hosts pubsub.googleapis.com &>/dev/null; then
        echo "  OK: pubsub.googleapis.com resolves"
    else
        echo "  FAIL: Cannot resolve pubsub.googleapis.com"; errors=$((errors+1))
    fi

    # 10. Jumpbox SSH connectivity
    echo "[10/10] Jumpbox SSH..."
    local jb_host=$(_parse_env_val "JUMPBOX_HOST" "$env_file" 2>/dev/null)
    if [[ -z "$jb_host" && -f "$REPO_ROOT/run_config.yml" ]]; then
        jb_host=$(grep 'jumpbox_host:' "$REPO_ROOT/run_config.yml" 2>/dev/null | awk '{print $2}')
    fi
    if [[ -n "$jb_host" ]]; then
        if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$jb_host" "echo ok" &>/dev/null; then
            echo "  OK: Can SSH to $jb_host"
        else
            echo "  WARN: Cannot SSH to $jb_host (may work from inside container)"
        fi
    else
        echo "  SKIP: Jumpbox host not configured"
    fi

    echo ""
    echo "=================================="
    if [[ "$errors" -gt 0 ]]; then
        echo "PREFLIGHT FAILED: $errors error(s) found. Fix them before starting."
        return 1
    else
        echo "PREFLIGHT PASSED: All checks OK."
        return 0
    fi
}

cmd_build() {
    echo "Building $IMAGE_NAME..."
    $RUNTIME build \
        -f "$SCRIPT_DIR/Containerfile.relay" \
        -t "$IMAGE_NAME" \
        "$REPO_ROOT"
    echo "Build complete."
}

cmd_start() {
    _read_env

    # Clean up previous state before starting
    cmd_clean

    # Run preflight checks before starting
    if ! cmd_preflight; then
        echo ""
        echo "Fix the errors above before starting the relay."
        return 1
    fi
    echo ""

    if $RUNTIME ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        echo "Container $CONTAINER_NAME is already running."
        echo "Use 'bash $0 restart' to restart, or 'bash $0 logs' to view output."
        return
    fi

    $RUNTIME rm -f "$CONTAINER_NAME" 2>/dev/null || true

    echo "Starting $CONTAINER_NAME..."

    local userns_flag=""
    if [[ "$RUNTIME" == "podman" ]]; then
        userns_flag="--userns=keep-id"
    fi

    local git_cred_mount=""
    if [[ -n "$GIT_CREDENTIALS_FILE" && -f "$GIT_CREDENTIALS_FILE" ]]; then
        git_cred_mount="-v $GIT_CREDENTIALS_FILE:/secrets/git-credentials:ro,Z"
    fi

    mkdir -p "$REPO_ROOT/logs"

    $RUNTIME run -d \
        --name "$CONTAINER_NAME" \
        --restart=unless-stopped \
        $userns_flag \
        -v "$REPO_ROOT:/repo:Z" \
        -v "$GOOGLE_APPLICATION_CREDENTIALS:/secrets/gcp-sa-key.json:ro,Z" \
        -v "$JUMPBOX_SSH_KEY:/secrets/ssh-key:ro,Z" \
        $git_cred_mount \
        -e "GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa-key.json" \
        -e "GCP_PUBSUB_PROJECT_ID=$GCP_PUBSUB_PROJECT_ID" \
        -e "DCI_CONTAINERIZED=1" \
        "$IMAGE_NAME"

    echo ""
    echo "Relay started. Waiting for container to stabilize..."
    echo "  Log file: $REPO_ROOT/logs/relay.log"
    echo "---"

    # Wait and verify the container stays alive
    sleep 5
    if ! $RUNTIME ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        echo ""
        echo "ERROR: Container died within 5 seconds of starting!"
        echo ""
        # Try to get logs from the dead container
        echo "=== Container logs ==="
        $RUNTIME logs "$CONTAINER_NAME" 2>/dev/null || echo "(no container logs available)"
        echo ""
        echo "=== Exit code ==="
        $RUNTIME inspect "$CONTAINER_NAME" --format '{{.State.ExitCode}}' 2>/dev/null || echo "(container already removed)"
        echo ""
        echo "=== Last 10 lines of relay.log ==="
        tail -10 "$REPO_ROOT/logs/relay.log" 2>/dev/null || echo "(no log file)"
        echo ""
        echo "=== Recent podman events ==="
        $RUNTIME events --filter event=died --filter container="$CONTAINER_NAME" --since 1m 2>/dev/null | tail -3
        echo ""
        echo "=== SELinux denials (last 2 min) ==="
        if command -v ausearch &>/dev/null; then
            sudo ausearch -m avc -ts recent 2>/dev/null | grep -i "denied\|dci\|relay\|repo" | tail -10 || echo "(none found or no sudo access)"
        elif command -v audit2allow &>/dev/null; then
            sudo audit2allow -a 2>/dev/null | head -10 || echo "(no sudo access)"
        else
            echo "(ausearch not available — check manually: sudo cat /var/log/audit/audit.log | grep denied)"
        fi
        echo ""
        echo "=== File permissions (logs/) ==="
        ls -laZ "$REPO_ROOT/logs/" 2>/dev/null | head -5
        echo ""
        echo "=== File permissions (repo root) ==="
        ls -la "$REPO_ROOT/" 2>/dev/null | grep -E "audit|logs|run_config|\.env|\.git" || echo "(no matching files)"
        echo ""
        echo "=== Disk space ==="
        df -h "$REPO_ROOT" 2>/dev/null | tail -1 || echo "(df failed)"
        echo ""
        echo "=== OOM kills (dmesg) ==="
        dmesg 2>/dev/null | grep -i "killed\|oom" | tail -5 || echo "(no access to dmesg or none found)"
        echo ""
        echo "=== Podman storage health ==="
        $RUNTIME system info 2>&1 | grep -i "error\|warning" | head -5 || echo "(OK)"
        echo ""
        echo "=== DNS resolution ==="
        $RUNTIME run --rm "$IMAGE_NAME" python -c "import socket; socket.getaddrinfo('pubsub.googleapis.com', 443); print('DNS OK')" 2>/dev/null || echo "(DNS check failed or image missing)"
        echo ""
        echo "=== UID mapping ==="
        echo "Host UID: $(id -u) ($(id -un))"
        echo "Container log owner: $(stat -c '%u:%g' "$REPO_ROOT/logs/relay.log" 2>/dev/null || echo 'no log file')"
        echo ""
        echo "Troubleshooting:"
        echo "  1. Run in foreground to see the error:  bash $0 start-fg"
        echo "  2. SELinux blocking volume mounts?      Try: sudo setenforce 0  (temporary)"
        echo "  3. File ownership wrong?                sudo chown -R \$(id -u) $REPO_ROOT/logs $REPO_ROOT/audit.jsonl"
        echo "  4. Container image stale?               bash $0 update"
        echo "  5. Podman storage corrupt?              podman system reset --force && bash $0 update"
        echo "  6. Disk full?                           df -h $REPO_ROOT"
        return 1
    fi

    # Container is alive after 5s, now tail logs
    echo "Container is running (pid: $($RUNTIME inspect "$CONTAINER_NAME" --format '{{.State.Pid}}' 2>/dev/null))."
    echo "Tailing logs (Ctrl+C to detach, container keeps running)..."
    echo "---"
    $RUNTIME logs -f "$CONTAINER_NAME"

    # If we get here, logs -f exited. Check if container is still running.
    if ! $RUNTIME ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        echo ""
        echo "WARNING: Container stopped while tailing logs."
        echo "=== Exit code ==="
        $RUNTIME inspect "$CONTAINER_NAME" --format '{{.State.ExitCode}}' 2>/dev/null || echo "(container already removed)"
        echo "=== Last 10 lines of relay.log ==="
        tail -10 "$REPO_ROOT/logs/relay.log" 2>/dev/null || echo "(no log file)"
        echo "=== SELinux denials ==="
        if command -v ausearch &>/dev/null; then
            sudo ausearch -m avc -ts recent 2>/dev/null | grep -i "denied" | tail -5 || echo "(none)"
        fi
        echo ""
        echo "Run 'bash $0 start-fg' to see the full error output."
    fi
}

cmd_stop() {
    echo "Stopping $CONTAINER_NAME..."
    $RUNTIME stop "$CONTAINER_NAME" 2>/dev/null || true
    $RUNTIME rm "$CONTAINER_NAME" 2>/dev/null || true
    echo "Stopped."
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_update() {
    echo "Pulling latest code..."
    cd "$REPO_ROOT"
    git pull --ff-only
    echo ""
    cmd_build
    echo ""
    cmd_restart
}

cmd_logs() {
    $RUNTIME logs -f "$CONTAINER_NAME"
}

cmd_status() {
    if $RUNTIME ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        echo "RUNNING"
        $RUNTIME ps --filter "name=$CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Created}}"
    else
        echo "STOPPED"
        # Check if it exists but is stopped
        if $RUNTIME ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
            echo ""
            echo "Container exists but is not running:"
            $RUNTIME ps -a --filter "name=$CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Created}}"
            echo "Exit code: $($RUNTIME inspect "$CONTAINER_NAME" --format '{{.State.ExitCode}}' 2>/dev/null)"
        fi
        # Check for recent crash-restart loops
        local recent_deaths
        recent_deaths=$($RUNTIME events --filter event=died --filter container="$CONTAINER_NAME" --since 5m 2>/dev/null | wc -l)
        if [[ "$recent_deaths" -gt 0 ]]; then
            echo ""
            echo "WARNING: $recent_deaths container death(s) in the last 5 minutes (crash loop?)"
            echo "Run 'bash $0 start-fg' to see the error."
        fi
    fi
}

cmd_start_fg() {
    # Foreground mode for systemd — runs attached so systemd tracks the PID.
    _read_env

    $RUNTIME rm -f "$CONTAINER_NAME" 2>/dev/null || true

    mkdir -p "$REPO_ROOT/logs"

    local userns_flag=""
    if [[ "$RUNTIME" == "podman" ]]; then
        userns_flag="--userns=keep-id"
    fi

    local git_cred_mount=""
    if [[ -n "$GIT_CREDENTIALS_FILE" && -f "$GIT_CREDENTIALS_FILE" ]]; then
        git_cred_mount="-v $GIT_CREDENTIALS_FILE:/secrets/git-credentials:ro,Z"
    fi

    exec $RUNTIME run --rm \
        --name "$CONTAINER_NAME" \
        $userns_flag \
        -v "$REPO_ROOT:/repo:Z" \
        -v "$GOOGLE_APPLICATION_CREDENTIALS:/secrets/gcp-sa-key.json:ro,Z" \
        -v "$JUMPBOX_SSH_KEY:/secrets/ssh-key:ro,Z" \
        $git_cred_mount \
        -e "GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa-key.json" \
        -e "GCP_PUBSUB_PROJECT_ID=$GCP_PUBSUB_PROJECT_ID" \
        -e "DCI_CONTAINERIZED=1" \
        "$IMAGE_NAME"
}

cmd_shell() {
    $RUNTIME exec -it "$CONTAINER_NAME" /bin/bash
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${1:-help}" in
    build)      cmd_build ;;
    start)      cmd_start ;;
    start-fg)   cmd_start_fg ;;
    stop)       cmd_stop ;;
    restart)    cmd_restart ;;
    update)     cmd_update ;;
    logs)       cmd_logs ;;
    status)     cmd_status ;;
    shell)      cmd_shell ;;
    preflight)  cmd_preflight ;;
    clean)      cmd_clean ;;
    *)
        echo "DCI Relay — Container Management"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  preflight Check all prerequisites before starting"
        echo "  clean     Kill hanging processes, remove orphan containers, fix permissions"
        echo "  build     Build the container image"
        echo "  start     Start the relay daemon (clean + preflight + detached)"
        echo "  start-fg  Start in foreground (used by systemd)"
        echo "  stop      Stop the relay daemon"
        echo "  restart   Stop + start"
        echo "  update    git pull + rebuild + restart"
        echo "  logs      Follow container logs"
        echo "  status    Show if relay is running"
        echo "  shell     Open bash inside the container"
        ;;
esac
