#!/usr/bin/env bash
# Relay diagnostic script — runs all health checks and reports results.
# Usage: ./scripts/diagnose.sh
#
# Replaces the manual runbook. Each check rules out one failure mode.
# Exit code: 0 = healthy, 1 = issue found (details printed).
set -uo pipefail

cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC} $1"; }
fail() { echo -e "  ${RED}FAIL${NC} $1"; ISSUES=$((ISSUES+1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; }

ISSUES=0

echo "=== DCI Relay Diagnostics ==="
echo ""

# ---------------------------------------------------------------
# 1. Local MCP subscription health
# ---------------------------------------------------------------
echo "[1/6] Pub/Sub subscription state"
SUB_DIAG=$(python3 -c "
from agents.bridge.pubsub_client import get_connection_diagnostics
import json
print(json.dumps(get_connection_diagnostics()))
" 2>/dev/null) || SUB_DIAG='{"diagnosis":"IMPORT_FAILED"}'

DIAGNOSIS=$(echo "$SUB_DIAG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('diagnosis','UNKNOWN'))" 2>/dev/null || echo "PARSE_FAILED")
LAST_PULL=$(echo "$SUB_DIAG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('last_successful_pull_seconds_ago','never'))" 2>/dev/null || echo "?")
LAST_RELAY=$(echo "$SUB_DIAG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('last_relay_response_seconds_ago','never'))" 2>/dev/null || echo "?")
PULL_ERRS=$(echo "$SUB_DIAG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pull_error_count',0))" 2>/dev/null || echo "?")

case "$DIAGNOSIS" in
    HEALTHY)           pass "Subscription: $DIAGNOSIS (last pull: ${LAST_PULL}s ago, last relay: ${LAST_RELAY}s ago)" ;;
    SUBSCRIPTION_MISSING) fail "Subscription: $DIAGNOSIS — run dci_preflight_check() or restart Claude Code" ;;
    PULL_STALE)        fail "Subscription: $DIAGNOSIS — last pull ${LAST_PULL}s ago. Subscription may be expired" ;;
    RELAY_SILENT)      warn "Subscription: $DIAGNOSIS — relay last responded ${LAST_RELAY}s ago" ;;
    PULL_ERRORS)       fail "Subscription: $DIAGNOSIS — ${PULL_ERRS} consecutive errors" ;;
    *)                 warn "Subscription: $DIAGNOSIS" ;;
esac

# ---------------------------------------------------------------
# 2. GCP credentials
# ---------------------------------------------------------------
echo "[2/6] GCP service account key"
SA_KEY="${PUBSUB_SA_KEY_PATH:-${GOOGLE_APPLICATION_CREDENTIALS:-}}"
if [[ -z "$SA_KEY" ]]; then
    SA_KEY="infra/dci-relay-sa-key.json"
fi
if [[ -f "$SA_KEY" ]]; then
    SA_SIZE=$(wc -c < "$SA_KEY" | tr -d ' ')
    if [[ "$SA_SIZE" -gt 100 ]]; then
        pass "SA key exists: $SA_KEY ($SA_SIZE bytes)"
    else
        fail "SA key too small: $SA_KEY ($SA_SIZE bytes) — may be corrupt"
    fi
else
    fail "SA key not found: $SA_KEY"
fi

# ---------------------------------------------------------------
# 3. Local git SHA vs remote
# ---------------------------------------------------------------
echo "[3/6] Code version"
LOCAL_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
REMOTE_SHA=$(git rev-parse --short origin/main 2>/dev/null || echo "unknown")
if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
    pass "Local ($LOCAL_SHA) matches origin/main ($REMOTE_SHA)"
else
    warn "Local ($LOCAL_SHA) differs from origin/main ($REMOTE_SHA) — unpushed commits?"
fi

# ---------------------------------------------------------------
# 4. Relay container (if accessible via MCP)
# ---------------------------------------------------------------
echo "[4/6] Relay reachability (via Pub/Sub)"
PING_RESULT=$(python3 -c "
import asyncio
from agents.bridge import pubsub_client as bridge
async def ping():
    try:
        r = await bridge.send_command('jumpbox.ping', {}, timeout=15)
        return r
    except Exception as e:
        return {'success': False, 'error': str(e)}
r = asyncio.run(ping())
import json
print(json.dumps(r))
" 2>/dev/null) || PING_RESULT='{"success":false,"error":"python failed"}'

PING_OK=$(echo "$PING_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success',False))" 2>/dev/null || echo "False")
RELAY_SHA=$(echo "$PING_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('relay_git_sha','unknown'))" 2>/dev/null || echo "unknown")
RELAY_UP=$(echo "$PING_RESULT" | python3 -c "import sys,json; v=json.load(sys.stdin).get('relay_uptime_seconds'); print(f'{v//60}m' if v else 'unknown')" 2>/dev/null || echo "unknown")

if [[ "$PING_OK" == "True" ]]; then
    pass "Relay reachable (SHA: $RELAY_SHA, uptime: $RELAY_UP)"
    if [[ "$RELAY_SHA" != "unknown" && "$RELAY_SHA" != "$LOCAL_SHA" ]]; then
        fail "Relay SHA ($RELAY_SHA) != local ($LOCAL_SHA) — run dci_relay_update() before dispatching"
    fi
else
    PING_ERR=$(echo "$PING_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','?')[:100])" 2>/dev/null || echo "?")
    fail "Relay unreachable: $PING_ERR"
fi

# ---------------------------------------------------------------
# 5. Running workflows
# ---------------------------------------------------------------
echo "[5/6] Running workflows"
WF_RESULT=$(python3 -c "
import asyncio
from agents.bridge import pubsub_client as bridge
async def check():
    try:
        r = await bridge.send_command('workflow.list', {}, timeout=15)
        return r
    except Exception as e:
        return {'success': False, 'error': str(e)}
r = asyncio.run(check())
import json
print(json.dumps(r))
" 2>/dev/null) || WF_RESULT='{"success":false}'

WF_OK=$(echo "$WF_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success',False))" 2>/dev/null || echo "False")
if [[ "$WF_OK" == "True" ]]; then
    WF_COUNT=$(echo "$WF_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "?")
    if [[ "$WF_COUNT" == "0" ]]; then
        pass "No workflows running"
    else
        warn "$WF_COUNT workflow(s) active — DO NOT restart relay"
    fi
else
    warn "Could not check workflows (relay may be down)"
fi

# ---------------------------------------------------------------
# 6. Pub/Sub free tier usage
# ---------------------------------------------------------------
echo "[6/6] Pub/Sub usage"
USAGE=$(python3 -c "
from agents.bridge import usage_tracker
s = usage_tracker.get_status()
print(f\"{s.get('used_mb',0):.1f}MB / {s.get('limit_mb',10240):.0f}MB ({s.get('percent',0):.1f}%)\")
" 2>/dev/null) || USAGE="unknown"
if [[ "$USAGE" != "unknown" ]]; then
    pass "Usage: $USAGE"
else
    warn "Could not read usage tracker"
fi

# ---------------------------------------------------------------
# 7. Container restart policy (relay machine)
# ---------------------------------------------------------------
echo "[7/8] Container restart policy"
if [[ "$PING_OK" == "True" ]]; then
    RESTART_POLICY=$(python3 -c "
import asyncio
from agents.bridge import pubsub_client as bridge
async def check():
    try:
        r = await bridge.send_command('jumpbox.execute',
            {'command': 'podman inspect dci-relay --format={{.HostConfig.RestartPolicy.Name}} 2>/dev/null || echo none'},
            timeout=15)
        return r.get('stdout','').strip()
    except:
        return 'unknown'
print(asyncio.run(check()))
" 2>/dev/null) || RESTART_POLICY="unknown"
    # Strip the remote output wrapper
    RESTART_POLICY=$(echo "$RESTART_POLICY" | grep -v "BEGIN REMOTE\|END REMOTE\|---" | tr -d '[:space:]')
    case "$RESTART_POLICY" in
        *unless-stopped*|*always*|*on-failure*) pass "Restart policy: $RESTART_POLICY" ;;
        *no*|*none*) fail "No restart policy — container won't recover from relay.update. Fix: recreate with --restart=unless-stopped" ;;
        *) warn "Could not determine restart policy: '$RESTART_POLICY'" ;;
    esac
else
    warn "Skipped (relay unreachable)"
fi

# ---------------------------------------------------------------
# 8. Hooks repo cloneable from jumpbox
# ---------------------------------------------------------------
echo "[8/8] Hooks repo access"
HOOKS_DIR=$(python3 -c "
import yaml
with open('run_config.yml') as f:
    rc = yaml.safe_load(f)
print(rc.get('jumpbox_hooks_dir', ''))
" 2>/dev/null) || HOOKS_DIR=""

if [[ "$HOOKS_DIR" == git@* || "$HOOKS_DIR" == https://* ]]; then
    if [[ "$PING_OK" == "True" ]]; then
        HOOKS_CHECK=$(python3 -c "
import asyncio
from agents.bridge import pubsub_client as bridge
async def check():
    try:
        r = await bridge.send_command('jumpbox.execute',
            {'command': 'GIT_SSH_COMMAND=\"ssh -o StrictHostKeyChecking=no\" git ls-remote $HOOKS_DIR HEAD 2>&1 | head -1'},
            timeout=20)
        return r
    except Exception as e:
        return {'success': False, 'error': str(e)}
import os; os.environ['HOOKS_DIR'] = '$HOOKS_DIR'
print(asyncio.run(check()))
" 2>/dev/null)
        # Simpler check: just verify via jumpbox
        HOOKS_RESULT=$(python3 -c "
import asyncio
from agents.bridge import pubsub_client as bridge
async def check():
    try:
        r = await bridge.send_command('jumpbox.execute',
            {'command': 'ssh -o StrictHostKeyChecking=no -o BatchMode=yes git@github-hooks echo ok 2>&1 | head -1'},
            timeout=15)
        stdout = r.get('stdout','')
        if 'successfully authenticated' in stdout.lower() or 'shell access' in stdout.lower():
            return 'ok'
        elif 'denied' in stdout.lower():
            return 'denied'
        else:
            return 'unknown'
    except:
        return 'error'
print(asyncio.run(check()))
" 2>/dev/null) || HOOKS_RESULT="error"
        case "$HOOKS_RESULT" in
            *ok*) pass "Hooks repo accessible: $HOOKS_DIR" ;;
            *denied*) fail "Hooks repo auth denied — check deploy key on jumpbox (~/.ssh/github_deploy_hooks)" ;;
            *) warn "Could not verify hooks access: $HOOKS_RESULT" ;;
        esac
    else
        warn "Skipped (relay unreachable)"
    fi
elif [[ -n "$HOOKS_DIR" ]]; then
    pass "Hooks dir is a local path: $HOOKS_DIR (no clone needed)"
else
    warn "No hooks dir configured in run_config.yml"
fi

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
echo ""
echo "=================================="
if [[ "$ISSUES" -eq 0 ]]; then
    echo -e "${GREEN}ALL CHECKS PASSED${NC}"
    exit 0
else
    echo -e "${RED}$ISSUES ISSUE(S) FOUND${NC}"
    exit 1
fi
