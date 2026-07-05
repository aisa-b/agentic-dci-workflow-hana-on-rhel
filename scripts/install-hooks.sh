#!/bin/bash
# Install git hooks for the agentic-dci-workflow repo.
# Idempotent — safe to re-run.

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    echo "ERROR: Not inside a git repository."
    exit 1
fi

HOOKS_DIR="$REPO_ROOT/.git/hooks"

cat > "$HOOKS_DIR/pre-push" << 'HOOK'
#!/bin/bash
# Notify the relay daemon to pull latest code after push.
# Fire-and-forget — errors are suppressed, never blocks the push.
cd "$(git rev-parse --show-toplevel)" || exit 0
python3 -c "
from agents.bridge.pubsub_client import notify_relay_update
notify_relay_update()
" 2>/dev/null &
exit 0
HOOK

chmod +x "$HOOKS_DIR/pre-push"
echo "Installed pre-push hook -> relay auto-update on every push"
