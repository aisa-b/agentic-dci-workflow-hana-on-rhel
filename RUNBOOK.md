# Operations Runbook

Quick reference for running, monitoring, and recovering from DCI workflows.

## Starting a Run

```bash
# In the project root:
claude
# Then:
/dci-run target-1 RHEL-10.2
```

For parallel runs on multiple servers, use `claude agents` and dispatch
each `/dci-run` as a separate session.

## Checking Relay Health

```
dci_relay_health()      # Pub/Sub connectivity, recent issues
dci_jumpbox_ping()      # SSH tunnel to jumpbox
dci_workflow_list()     # Currently running workflows
```

If relay health fails:
1. Check the relay daemon: `dci_jumpbox_execute("ps aux | grep dci")`
2. Update/restart relay: `dci_relay_update()`
3. Verify: `dci_jumpbox_ping()`

## Monitoring a Running Workflow

Poll with `dci_workflow_status(target_host="target-1.example.corp")`.

The relay sends heartbeat messages every 120 seconds during workflow runs.
If a failure is detected, a heartbeat is sent immediately (async, without
waiting for the next interval). A heartbeat age over 180 seconds may indicate
the relay or workflow is stuck.

Default durations (static fallbacks for unknown servers):
- Phase 1 (OS Deployment): 20-40 min
- Phase 2 (OS Prep for HANA): 10-20 min
- Phase 3 (HANA Installation): 15-25 min
- Phase 4 (PBO Install and Run): 50-75 min
- Phase 5 (Results): 2-5 min

These are adaptive: `agents/local/phase_expectations.py` learns per-server
and per-RHEL-topic timing baselines from historical runs stored in
`phase_timings.json`. After 3+ runs on a server, the system uses the p90
percentile from that server's history instead of the static defaults. Different
hardware (HPE vs Lenovo) and different RHEL versions install at different speeds.

If a phase exceeds 1.5x its learned (or default) duration, investigate immediately.

## Stopping a Workflow

```
dci_workflow_stop(target_host="target-1.example.corp")  # Stop one
dci_workflow_stop_all()                                 # Stop all
```

Never restart the relay while a workflow is running -- it kills the SSH
tunnel and the workflow.

## Common Failures and Recovery

### Relay unreachable / Pub/Sub timeout

When any MCP tool returns "Timeout" or "relay not responding", follow these
steps **in order**. Each step rules out a specific failure mode.

**Step 1: Read the diagnostic error.** The timeout error now includes a
`_connection_state` dict with `diagnosis`. Possible values:
- `SUBSCRIPTION_MISSING` — no subscription exists. Fix: call `dci_preflight_check()`
- `PULL_STALE` — subscription exists but pulls return nothing. Likely expired (24h TTL). Fix: restart Claude Code for fresh MCP server
- `RELAY_SILENT` — pulls work but relay hasn't responded in 5+ min. Relay is down. Go to Step 2
- `PULL_ERRORS` — consecutive pull failures. Likely GCP credential issue. Go to Step 4
- `HEALTHY` — subscription and pulls fine, relay just slow. Wait or retry

**Step 2: Check relay container on the relay machine.**
```bash
podman ps -a --filter name=dci-relay
podman logs dci-relay |& tail -50
```
Look for: crash loops (multiple restarts), SSH errors, Python tracebacks.

**Step 3: Check the relay's git version.**
```bash
# After relay is reachable:
dci_jumpbox_ping()  # response includes relay_git_sha and relay_uptime_seconds
```
Compare `relay_git_sha` to local `git rev-parse --short HEAD`. If different,
the relay is running old code — call `dci_relay_update()`.

**Step 4: Check GCP credentials.**
```bash
# On the relay machine:
podman exec dci-relay python3 -c "
from google.cloud import pubsub_v1
sub = pubsub_v1.SubscriberClient()
print(list(sub.list_subscriptions(request={'project': 'projects/<your-gcp-project>'})))
"
```
If this fails with auth errors, the SA key is expired or missing.

**Step 5: Check subscription exists.**
```bash
# On operator machine:
python3 -c "
from agents.bridge.pubsub_client import get_connection_diagnostics
import json
print(json.dumps(get_connection_diagnostics(), indent=2))
"
```

**DO NOT** call `dci_relay_update()` as the first step. That restarts the
relay container and kills any running workflow. Only update after confirming
no workflows are in progress.

### SSH auth failure on target
After fresh OS deployment, the password resets to `<default-password>`.
The relay tries fallback passwords automatically. If all fail:
1. Check `DCI_TARGET_PASSWORD` in relay's `.env`
2. Check `DCI_FALLBACK_PASSWORDS` in relay's `.env`
3. Verify the target is actually reachable: `dci_ssh_diagnostics()`

### Stale host key
Happens after every OS deployment (new SSH key). The relay auto-clears
stale keys before each run. If you hit this manually:
```
dci_jumpbox_execute("ssh-keygen -R target-1.example.corp")
```

### Settings sync failure
If `dci_workflow_run()` fails with "missing disk_map":
1. Run `/dci-configure --discover <hostname>` to populate the disk map
2. Verify with `/dci-configure show`

### Git push rejected
The agent only pushes to `github.com/aisa-b/agentic-dci-workflow`.
If push fails:
1. Check for conflicting changes: `git status`, `git log --oneline -5`
2. Pull and rebase: `git pull --rebase origin main`

## Configuration

### Where settings live
| Setting | Location | Example |
|---------|----------|---------|
| Target server, RHEL topic | `run_config.yml` | `target: target-1.example.corp` |
| LLM model | `run_config.yml` | `model: claude-opus-4-6` |
| Disk mappings | `run_config.yml` (disk_map) | `target-1: scsi-3<disk-id>` |
| GCP project IDs | `.env` | `GCP_PUBSUB_PROJECT_ID=<your-gcp-project>` |
| SA key path | `.env` | `PUBSUB_SA_KEY_PATH=/path/to/key.json` |
| Target password | `.env` (relay only) | `DCI_TARGET_PASSWORD=<default-password>` |

### Adding a new server
1. Run `/dci-configure --discover <hostname>` to find install disks
2. Add the server entry to `run_config.yml` under `servers:`
3. Commit and push
4. Run `/dci-run <hostname> <topic>`

## Log Files

| File | Location | Content |
|------|----------|---------|
| Agent audit | `/tmp/dci-agent-logs/agent_audit.jsonl` | Tool calls and results |
| Run journal | `/tmp/dci-agent-logs/run_journal.jsonl` | Per-run lifecycle events |
| Unified events | `/tmp/dci-agent-logs/events.jsonl` | All events from all sources |
| Knowledge base | `/tmp/dci-agent-logs/knowledge_base.json` | Past fixes and patterns |
| Relay KB | `/tmp/dci-agent-logs/relay_kb.json` | Relay/infra issues |

## Emergency Procedures

### Kill all agent activity
```
dci_workflow_stop_all()
```

### Revert all agent changes
```bash
git log --oneline | grep '\[agent-fix'   # find agent commits
git revert --no-edit <sha>               # revert each one
git push origin HEAD
```

### Relay daemon won't start

**Step 1: Run preflight checks**
```bash
cd ~/dci-agent
bash container/relay.sh preflight
```
This checks: .env, SA key, SSH key, run_config.yml, log permissions,
container image, Podman storage, DNS, and jumpbox SSH.

**Step 2: Run in foreground for debugging**
```bash
bash container/relay.sh start-fg
```
This shows the full output including Python tracebacks.

**Step 3: Common issues and fixes**

| Symptom | Cause | Fix |
|---|---|---|
| Container exits with code 0 immediately | `set -e` in entrypoint or pipe failure | Use `start-fg`, update to latest code |
| `overlay-containers ... no such file` | Stale Podman storage | `rm -rf /var/tmp/<user>-containers/run; podman system migrate` |
| Container crash-loops (dies every 7s) | `--restart unless-stopped` + crash | `systemctl --user stop dci-relay.service; podman rm -f dci-relay` |
| Log file owned by 100000:100000 | UID mapping with `--userns=keep-id` | `sudo chown -R $(id -u) ~/dci-agent/logs` |
| Podman storage on NFS fails | overlayfs incompatible with NFS | Move storage to local fs (see GETTING_STARTED.md step 6.5) |
| `git pull` fails inside container | No git credentials mounted | Mount `~/.git-credentials` via `GIT_CREDENTIALS_FILE` in `.env` |

**Step 4: Nuclear reset**
```bash
systemctl --user stop dci-relay.service
podman rm -f dci-relay
rm -rf /var/tmp/<user>-containers/run /var/tmp/<user>-containers/storage/libpod
podman system migrate
bash container/relay.sh update
```

## Relay Deployment (after code changes)

When code changes affect the relay (config_loader.py, relay/*.py, .env handling),
the relay machine needs to be updated manually. The MCP tool `dci_relay_update()`
only does `git pull` and daemon restart. It does NOT create missing files like `.env`.

### First-time `.env` setup on relay-host

After DCI-057 (passwords moved from git to .env), the relay requires a `.env` file.
SSH into relay-host and create it:

```bash
ssh relay-host
cd /path/to/agentic-dci-workflow
cat > .env << 'EOF'
# Relay-side secrets (not in git)
GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"
GOOGLE_APPLICATION_CREDENTIALS="/path/to/infra/dci-relay-sa-key.json"
JUMPBOX_SSH_KEY="/path/to/.ssh/id_ed25519"
DCI_TARGET_PASSWORD="<default-password>"
DCI_FALLBACK_PASSWORDS="<password1>,<password2>"
EOF
```

Then pull the latest code and restart:
```bash
git pull --ff-only
container/relay.sh restart
```

Verify with `dci_relay_health()` and `dci_jumpbox_ping()` from your local machine.

### After any code change

If you push code that changes relay behavior:
1. `dci_relay_update()` from your local machine (pulls code + restarts daemon)
2. Verify with `dci_jumpbox_ping()`
3. If `dci_relay_update()` itself fails (relay is down), SSH in manually:
   ```bash
   ssh relay-host
   cd /path/to/agentic-dci-workflow
   git pull --ff-only
   container/relay.sh restart
   ```

### Relay Test Suite

The relay has a comprehensive test suite (533 tests) covering safety validation,
settings sync, SSH allowlists, and handler behavior. Before pushing relay changes:

```bash
pytest tests/ -q
```

The test suite runs in CI on every push. If tests fail, fix before pushing.

## Troubleshooting

### MCP server fails
```bash
.venv/bin/python -c "from agents.mcp_server import mcp; print('OK')"
```

### Python module not found
```bash
source .venv/bin/activate
python3 -c "import agents; print('OK')"
```

### Deferred ACK behavior (short commands)

Short-running commands (SSH execute, jumpbox ping, etc.) use deferred ACK:
the Pub/Sub message is acknowledged only AFTER the result is published back.
If the relay crashes mid-command, the message is redelivered automatically.
This means you may see duplicate results for commands that were interrupted --
the correlation ID matching handles this gracefully.
