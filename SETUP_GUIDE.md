# DCI Multi-Agent System -- Setup Guide

This guide walks you through setting up the cross-network agent architecture
from scratch. Follow every step in order. Each section tells you which machine
to run the commands on.

---

## Table of Contents

1. [Architecture Recap](#1-architecture-recap)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 -- Google Cloud Project Setup](#3-step-1----google-cloud-project-setup)
4. [Step 2 -- Create Pub/Sub Topics and Subscriptions](#4-step-2----create-pubsub-topics-and-subscriptions)
5. [Step 3 -- Create a Service Account](#5-step-3----create-a-service-account)
6. [Step 4 -- Set Up Your Local Machine (Agent Side)](#6-step-4----set-up-your-local-machine-agent-side)
7. [Step 5 -- Set Up relay-host (Relay Container)](#7-step-5----set-up-acswdf031933-relay-side)
8. [Step 6 -- Verify the Jumpbox (jumpbox)](#8-step-6----verify-the-jumpbox-jumpbox)
9. [Step 7 -- Test the Pub/Sub Connection](#9-step-7----test-the-pubsub-connection)
10. [Step 8 -- Test the Relay SSH Connection](#10-step-8----test-the-relay-ssh-connection)
11. [Step 9 -- Run the Relay Container](#11-step-9----run-the-relay-daemon)
12. [Step 10 -- Run the Agent (Claude Code CLI)](#12-step-10----run-the-agent)
13. [Troubleshooting](#13-troubleshooting)
14. [Security Checklist](#14-security-checklist)

---

## 1. Architecture Recap

Three machines, two networks, one Google Cloud bridge.
Multiple target servers are supported in parallel (target-1, target-2, target-4, etc.):

```
Your Machine (Operator)                              Remote Network
 |                                                    |
 |  Claude Code CLI + MCP tools                       |  relay-host (relay daemon)
 |  File edits + git ops happen locally               |    - Runs in a Podman container (UBI9)
 |  Sends commands via Pub/Sub                        |    - Subscribes to Pub/Sub
 |  Receives results via Pub/Sub                      |    - MCP tool handlers
 |  Local clone of hooks repo                         |    - Sends results via Pub/Sub
 |                                                    |
 |          Google Cloud Pub/Sub                      |  Jumpbox jumpbox
 |              (bridge)                              |    - Runs dci-rhel-agent-ctl
 |                                                    |    - Repo: /agentic-dci-workflow/
 |                                                    |    - SSHes to target servers
 |                                                    |
 |                                                    |  Target Servers (target-1, target-2, ...)
 |                                                    |    - Bare metal SAP HANA
```

**What runs where:**

| Machine              | What it runs                          | What it needs                          |
|----------------------|---------------------------------------|----------------------------------------|
| Your machine         | Claude Code CLI with MCP, file/git ops (local) | Python 3.10+, GCP credentials, `claude` CLI |
| relay-host         | Relay daemon (Podman container)       | Podman, GCP credentials, SSH key to jumpbox |
| Jumpbox (jumpbox)     | Nothing new                           | Already has dci-rhel-agent-ctl, git, SSH keys |
| Target servers       | Nothing new                           | Already configured by DCI              |

---

## 2. Prerequisites

Before you start, make sure you have:

- [ ] A Google Cloud account with billing enabled
- [ ] `gcloud` CLI installed on your machine ([install guide](https://cloud.google.com/sdk/docs/install))
- [ ] Python 3.10 or newer on your machine
- [ ] Podman (or Docker) installed on the relay machine
- [ ] SSH access from the relay machine to the jumpbox (jumpbox) as user `<jumpbox-user>`
- [ ] The repo cloned on the jumpbox at `/agentic-dci-workflow/`
- [ ] A local clone of the repo on your machine (file edits and git ops happen locally)
- [ ] Claude Code CLI installed (`npm install -g @anthropic-ai/claude-code`)

---

## 3. Step 1 -- Google Cloud Project Setup

**Run on: Your local machine**

### 3.1. Authenticate with Google Cloud

```bash
gcloud auth login
```

This opens a browser. Log in with the Google account that has access to
your GCP project.

### 3.2. Set your project

```bash
# Replace with your actual project ID
export GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"

gcloud config set project "$GCP_PUBSUB_PROJECT_ID"
```

### 3.3. Enable the Pub/Sub API

```bash
gcloud services enable pubsub.googleapis.com
```

**What just happened:** You told Google Cloud to activate the Pub/Sub
messaging service for your project. This is a one-time step.

### 3.4. Verify it worked

```bash
gcloud services list --enabled | grep pubsub
```

You should see: `pubsub.googleapis.com    Cloud Pub/Sub API`

---

## 4. Step 2 -- Create Pub/Sub Topics and Subscriptions

**Run on: Your local machine**

### 4.1. What are topics and subscriptions?

- A **topic** is a named channel for messages. Think of it like a mailbox.
- A **subscription** is a listener attached to a topic. It receives copies of
  messages published to that topic.

We need two topics:
- `dci-commands` -- your machine publishes commands, the relay receives them
- `dci-results` -- the relay publishes results, your machine receives them

### 4.2. Create the topics

```bash
# Commands topic (Operator -> Relay)
gcloud pubsub topics create dci-commands \
    --message-retention-duration=600s

# Results topic (Relay -> Operator)
gcloud pubsub topics create dci-results \
    --message-retention-duration=600s
```

The `--message-retention-duration=600s` means messages are kept for 10 minutes
max. This prevents sensitive data from lingering in Google Cloud.

### 4.3. Create the subscriptions

```bash
# The relay subscribes to commands
gcloud pubsub subscriptions create dci-commands-relay-sub \
    --topic=dci-commands \
    --ack-deadline=600 \
    --message-retention-duration=600s \
    --expiration-period=never

# Your machine subscribes to results
gcloud pubsub subscriptions create dci-results-agent-sub \
    --topic=dci-results \
    --ack-deadline=120 \
    --message-retention-duration=600s \
    --expiration-period=never
```

**What do these flags mean?**

| Flag                          | Meaning                                                |
|-------------------------------|--------------------------------------------------------|
| `--ack-deadline=600`          | The relay has 10 minutes to process a command before Pub/Sub re-delivers it. DCI workflows can take a long time, so we set this high. |
| `--message-retention-duration=600s` | Messages older than 10 minutes are automatically deleted. |
| `--expiration-period=never`   | The subscription never expires (even if unused for a while). |

### 4.4. Verify

```bash
gcloud pubsub topics list
gcloud pubsub subscriptions list
```

You should see both topics and both subscriptions.

---

## 5. Step 3 -- Create a Service Account

**Run on: Your local machine**

A service account is like a "robot user" that your code uses to authenticate
with Google Cloud. Both the operator machine and the relay use the same service account.

### 5.1. Create the service account

```bash
gcloud iam service-accounts create dci-agent-relay \
    --display-name="DCI Agent Relay Service Account"
```

### 5.2. Grant it Pub/Sub permissions

```bash
SA_EMAIL="dci-agent-relay@${GCP_PUBSUB_PROJECT_ID}.iam.gserviceaccount.com"

# Grant Pub/Sub Editor — covers publish, subscribe, AND create/delete subscriptions.
# The MCP server creates per-session temporary subscriptions to receive results,
# which requires pubsub.subscriptions.create (not included in publisher/subscriber roles).
gcloud projects add-iam-policy-binding "$GCP_PUBSUB_PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/pubsub.editor" \
    --condition=None \
    --quiet
```

### 5.3. Download the key file

```bash
mkdir -p infra
gcloud iam service-accounts keys create infra/dci-relay-sa-key.json \
    --iam-account="$SA_EMAIL"
```

**IMPORTANT:** This JSON file is a credential. Treat it like a password.
- Do NOT commit it to git (it is already in `.gitignore`)
- You will need to copy it to the relay machine later

### 5.4. Verify the key works

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/infra/dci-relay-sa-key.json"

# Quick test: list topics using the service account
gcloud pubsub topics list
```

If you see your two topics, the key is working.

---

## 6. Step 4 -- Set Up Your Local Machine (Agent Side)

**Run on: Your local machine (macOS or Linux)**

### 6.1. Clone the repo (if not already done)

```bash
cd ~/ALL_PROJECTS
git clone https://github.com/aisa-b/agentic-dci-workflow.git agentic-dci-workflow
cd agentic-dci-workflow
```

### 6.2. Install Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
```

Verify it works:

```bash
claude --version
```

This is the primary interface for running the agent. It reads `.mcp.json`
from the repo root to discover the MCP tools.

### 6.3. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 6.4. Install dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `claude-agent-sdk` -- the Claude Agent SDK for building the agent
- `google-cloud-pubsub` -- the Pub/Sub client library

### 6.5. Set up your `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your values. The `.env` only contains **secrets** -- all run
settings (target server, model, etc.) are in `run_config.yml` instead:

```bash
# Claude via Vertex AI
ANTHROPIC_VERTEX_PROJECT_ID="<your-vertex-project>"

# Pub/Sub project (separate from Vertex AI)
GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"

# Local repo clone path
DCI_LOCAL_REPO_ROOT="."

# Logging
DCI_LOG_DIR="/tmp/dci-agent-logs"
```

### 6.6. Review `run_config.yml`

All run settings are in `run_config.yml` (committed to git). Review it and
adjust if needed:

```bash
cat run_config.yml
```

Key fields:
- `target` -- which server to test (e.g., `target-1.example.corp`)
- `model` -- which Claude model to use (e.g., `claude-opus-4-6`)
- `max_fix_attempts` -- how many retries before giving up

### 6.7. Verify Claude access

```bash
# Make sure you can authenticate with Vertex AI
gcloud auth application-default login
```

This opens a browser. Authenticate with the account that has access to
Claude on Vertex AI.

---

## 7. Step 5 -- Set Up relay-host (Relay Side)

**Run on: relay-host (relay machine)**

The relay now runs in a Podman container based on UBI9 with Python 3.12.
No Python venv setup is needed on the host -- everything is inside the container.

**IMPORTANT:** relay-host uses `tcsh` as the default shell. Either switch to
bash first (`bash`) and use `export VAR=val`, or use `setenv VAR val` for tcsh.
All instructions below assume you run `bash` first.

### 7.1. Clone the repo

```bash
bash
git config --global credential.helper store
git clone https://github.com/aisa-b/agentic-dci-workflow.git \
    /sapmnt/home/<your-username>/Desktop/multi-agent-dci
cd /sapmnt/home/<your-username>/Desktop/multi-agent-dci
```

When prompted, enter your GitHub username (`<your-github-user>`) and a Personal Access
Token (PAT) with `repo` scope as the password. The token is stored in
`~/.git-credentials` (readable only by your user).

### 7.2. Copy the GCP service account key

There is no direct SCP from your machine to relay-host (different networks).
Two methods:

**Method A -- Google Cloud Console (recommended, no clipboard needed):**

1. Inside Citrix, open a browser
2. Go to https://console.cloud.google.com/iam-admin/serviceaccounts?project=<your-gcp-project>
3. Click `dci-agent-relay` → **Keys** tab → **Add Key** → **Create new key** → **JSON**
4. File downloads directly to relay-host
5. Move it:

```bash
mv ~/Downloads/<your-gcp-project>-*.json /sapmnt/home/<your-username>/Desktop/multi-agent-dci/dci-relay-sa-key.json
```

**Method B -- Manual copy-paste via Citrix:**

1. On your machine: `cat infra/dci-relay-sa-key.json` (copy from terminal)
2. On relay-host: `vi dci-relay-sa-key.json` → `i` → paste → `Esc` → `:wq`

**NEVER paste the key into chat, email, or Slack.**

### 7.3. Create the relay `.env` file

The relay `.env` only contains **per-machine secrets**. All run settings
(target server, jumpbox host, topic names, etc.) come from `run_config.yml`
which the relay already has from the git clone.

Create `/sapmnt/home/<your-username>/Desktop/multi-agent-dci/.env`:

```bash
# Machine-specific secrets only (everything else is in run_config.yml)
GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"
GOOGLE_APPLICATION_CREDENTIALS="/sapmnt/home/<your-username>/Desktop/multi-agent-dci/dci-relay-sa-key.json"
JUMPBOX_SSH_KEY="/sapmnt/home/<your-username>/.ssh/id_ed25519"
```

That's it -- 3 lines. To change the target server, model, or any other
run setting, edit `run_config.yml` locally, commit, push. The relay
picks it up via `git pull` before each workflow run.

### 7.4. Build the relay container

```bash
cd /sapmnt/home/<your-username>/Desktop/multi-agent-dci
bash container/relay.sh build
```

This builds a container image called `dci-relay` from `container/Containerfile.relay`.
The image is based on UBI9 with Python 3.12 and includes only the relay dependencies
(`google-cloud-pubsub`, `paramiko`, `python-dotenv`). No LLM libraries.

### 7.5. Test SSH to the jumpbox

```bash
ssh -i /sapmnt/home/<your-username>/.ssh/id_ed25519 <jumpbox-user>@jumpbox "hostname && echo OK"
```

You should see `jumpbox` and `OK`. If this fails, fix SSH access before
proceeding.

### 7.6. Test SSH to the target (two-hop)

```bash
ssh -J <jumpbox-user>@jumpbox root@target-1.example.corp "hostname && echo OK"
```

This connects through the jumpbox to the target. You should see the target
hostname and `OK`.

If your SSH config doesn't support `-J`, try:

```bash
ssh <jumpbox-user>@jumpbox "ssh root@target-1.example.corp 'hostname && echo OK'"
```

---

## 8. Step 6 -- Verify the Jumpbox (jumpbox)

**Run on: Jumpbox (via SSH from relay machine)**

The jumpbox needs NO changes. Just verify things are in place:

```bash
ssh <jumpbox-user>@jumpbox
```

### 8.1. Check the repo

```bash
cd /agentic-dci-workflow
git status
git remote -v
```

You should see a clean repo with a remote pointing to
`github.com/aisa-b/agentic-dci-workflow`. The repo uses a read-only
SSH deploy key (the jumpbox only pulls, never pushes).

### 8.2. Check the hooks directory

```bash
ls /agentic-dci-workflow/dci-hooks/
```

This is the Ansible hooks directory used by `dci-rhel-agent-ctl`.

### 8.3. Check dci-rhel-agent-ctl

```bash
which dci-rhel-agent-ctl
dci-rhel-agent-ctl --help 2>&1 | head -5
```

### 8.4. Check SSH to a target server

```bash
ssh root@target-1.example.corp "hostname && cat /etc/redhat-release"
```

Note: the target gets redeployed on every DCI run, so the host key changes
each time. The relay auto-clears stale host keys before each run.

### 8.5. Exit the jumpbox

```bash
exit
```

---

## 9. Step 7 -- Test the Pub/Sub Connection

**Run on: Your local machine first, then relay machine**

This test verifies that messages flow between your machine and the Linux machine
through Google Cloud.

### 9.1. On your local machine -- publish a test message

```bash
source .venv/bin/activate
export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/infra/dci-relay-sa-key.json"

python3 -c "
from google.cloud import pubsub_v1
import json, os

publisher = pubsub_v1.PublisherClient()
topic = f'projects/{os.environ[\"GCP_PUBSUB_PROJECT_ID\"]}/topics/dci-commands'

data = json.dumps({'test': True, 'message': 'Hello from operator'}).encode()
future = publisher.publish(topic, data)
print(f'Published message ID: {future.result()}')
print('OK - message sent to dci-commands topic')
"
```

### 9.2. On relay-host -- receive the test message

Start the relay container first (Step 9), then open a shell inside it:

```bash
bash container/relay.sh shell
```

Inside the container shell, run:

```bash
python3 -c "
from google.cloud import pubsub_v1
import json, os

subscriber = pubsub_v1.SubscriberClient()
sub = f'projects/{os.environ[\"GCP_PUBSUB_PROJECT_ID\"]}/subscriptions/dci-commands-relay-sub'

response = subscriber.pull(subscription=sub, max_messages=1, timeout=10)
for msg in response.received_messages:
    data = json.loads(msg.message.data.decode())
    print(f'Received: {data}')
    subscriber.acknowledge(subscription=sub, ack_ids=[msg.ack_id])
    print('OK - message acknowledged')

if not response.received_messages:
    print('No messages received. Check topic/subscription names and credentials.')
"
```

If both steps work, the bridge is functional.

### 9.3. Test the reverse direction

Repeat the above but swap roles: publish from relay-host to
`dci-results`, receive on your machine from `dci-results-agent-sub`.

---

## 10. Step 8 -- Test the Relay SSH Connection

**Run on: relay-host**

Before starting the relay container, verify the host machine can SSH to
the jumpbox and run commands:

```bash
# Test SSH to jumpbox
ssh -i /sapmnt/home/<your-username>/.ssh/id_ed25519 <jumpbox-user>@jumpbox \
    "cd /agentic-dci-workflow && git status && echo 'SSH to jumpbox: OK'"

# Test two-hop SSH to target
ssh -i /sapmnt/home/<your-username>/.ssh/id_ed25519 <jumpbox-user>@jumpbox \
    "ssh -o StrictHostKeyChecking=no root@target-1.example.corp hostname && echo 'Two-hop SSH: OK'"
```

---

## 11. Step 9 -- Run the Relay Daemon

**Run on: relay-host**

The relay runs as a Podman container. The `container/relay.sh` script manages
the full lifecycle. The container mounts:

- The repo at `/repo:Z` (for `run_config.yml`, git pull, and code updates)
- GCP service account key at `/secrets/gcp-sa-key.json`
- SSH key at `/secrets/ssh-key`
- Git credentials at `/secrets/git-credentials` (if available)

All file editing and git operations happen locally -- the relay
never touches files or runs git commands (except `git pull` to sync the repo).

### 11.1. Start the relay

```bash
cd /sapmnt/home/<your-username>/Desktop/multi-agent-dci
bash container/relay.sh start
```

This starts the container in detached mode and tails the logs. Press `Ctrl+C`
to detach -- the container keeps running in the background.

The container is configured with `--restart unless-stopped`, so it
automatically restarts after crashes or machine reboots.

### 11.2. Common relay commands

```bash
# View logs (follow mode)
bash container/relay.sh logs

# Check if running
bash container/relay.sh status

# Stop the relay
bash container/relay.sh stop

# Restart (stop + start)
bash container/relay.sh restart

# Update: git pull + rebuild image + restart container
bash container/relay.sh update

# Open a shell inside the running container
bash container/relay.sh shell
```

### 11.3. How the container works

The entrypoint (`container/entrypoint.sh`) does the following on startup:

1. Copies the mounted SSH key to `/tmp/.ssh/id_key` with strict permissions
2. If the repo is mounted at `/repo`, sets it as the working directory
3. Runs `git pull --ff-only` to get the latest code
4. Starts `python -u -m relay.daemon` with output to both stdout and a log file

Logs are written to `logs/relay.log` in the repo directory and are also
available via `bash container/relay.sh logs` (which runs `podman logs -f`).

### 11.4. Optional: systemd integration

To manage the container via systemd (survives logout):

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/dci-relay.service << 'EOF'
[Unit]
Description=DCI Agent Relay Daemon (container)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/sapmnt/home/<your-username>/Desktop/multi-agent-dci
ExecStart=/bin/bash container/relay.sh start-fg
ExecStop=/bin/bash container/relay.sh stop
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable dci-relay
systemctl --user start dci-relay

# Check status
systemctl --user status dci-relay

# View logs
journalctl --user -u dci-relay -f
```

To make the service survive logout:

```bash
loginctl enable-linger $(whoami)
```

---

## 12. Step 10 -- Run the Agent

**Run on: Your local machine**

The interface is **Claude Code CLI** (`claude`) with MCP integration.

### 12.1. Start with Claude Code CLI (recommended)

```bash
cd ~/ALL_PROJECTS/agentic-dci-workflow
claude
```

This starts an interactive Claude session with 15 MCP tools (defined in
`.mcp.json`), 4 skills, and 4 subagents available. From the prompt you can:

```
# Run a full autonomous workflow on a target server
/dci-run target-1

# Run on multiple servers in parallel (use claude agents)
# Each /dci-run runs in its own session
/dci-run target-1
/dci-run target-2

# Configure a new server (one-time disk discovery)
/dci-configure --discover target-5

# Apply a single targeted fix
/dci-fix "sap-preconfigure role version mismatch"

# Generate a failure report PR
/dci-report
```

### 12.2. MCP tools available

The MCP server (`agents/mcp_server.py`) provides 15 tools to Claude Code:

| Tool                    | What it does                                                |
|-------------------------|-------------------------------------------------------------|
| `dci_preflight_check`   | Refresh Pub/Sub subscription, verify relay health, ping jumpbox |
| `dci_workflow_run`      | Trigger full DCI pipeline (OS deploy + SAP prep + benchmark + results) |
| `dci_workflow_status`   | Poll for workflow progress and final result                 |
| `dci_workflow_list`     | List all currently running workflows                        |
| `dci_workflow_stop`     | Stop a specific running workflow by target hostname         |
| `dci_workflow_stop_all` | Stop all running workflows                                  |
| `dci_fleet_status`      | Unified fleet dashboard: all workflows, phases, alerts      |
| `dci_ssh_execute`       | Run a read-only command on the target server via SSH        |
| `dci_ssh_diagnostics`   | Run built-in diagnostic suite with focus area hint          |
| `dci_check_events`      | Check for workflow completion/failure events                |
| `dci_jumpbox_execute`   | Run a command on the jumpbox (jumpbox) directly              |
| `dci_jumpbox_ping`      | Check relay/jumpbox connectivity                            |
| `dci_relay_update`      | Pull latest code on relay and restart the daemon            |
| `dci_relay_health`      | Show relay infrastructure health and stats                  |
| `dci_server_profile`    | Capture and persist target server hardware/OS profile       |

### 12.4. Skills

Skills are invoked via `/skill-name` in the Claude Code prompt:

| Skill              | What it does                                                       |
|--------------------|--------------------------------------------------------------------|
| `/dci-run`         | Full autonomous workflow: generate settings, run, diagnose, fix, retry (up to 5 attempts) |
| `/dci-configure`   | Disk discovery for new servers, show current disk_map and server status |
| `/dci-fix`         | Apply a single targeted fix based on a known error                 |
| `/dci-report`      | Generate failure report PR, revert all changes                     |

### 12.5. Subagents

Subagents are delegated to with "use the X subagent" in conversation:

| Subagent             | What it does                                                    |
|----------------------|-----------------------------------------------------------------|
| `dci-diagnostician`  | Exhaustive read-only diagnosis of failures (SRE perspective)   |
| `ansible-reviewer`   | Review Ansible changes for correctness before committing        |
| `hana-expert`        | SAP HANA installation and runtime health assessment             |
| `os-deploy-expert`   | Phase 1 OS deployment specialist (kickstart, PXE, partitioning) |

### 12.6. Monitor what's happening

Watch the agent output in Claude Code. On the relay side (relay-host),
you can see commands being processed:

```bash
bash container/relay.sh logs
```

Sample relay output:

```
[Relay] Received command: workflow.run (corr: abc-123)
[Relay] Executing on jumpbox: dci-rhel-agent-ctl ...
[Relay] Command completed (exit_code=1, 847s)
[Relay] Published result for abc-123
```

---

## 13. Troubleshooting

### "No messages received" on Pub/Sub test

1. Check that `GCP_PUBSUB_PROJECT_ID` is the same on both machines
2. Check that `GOOGLE_APPLICATION_CREDENTIALS` points to a valid key file
3. Verify the subscription exists: `gcloud pubsub subscriptions list`
4. Verify the topic exists: `gcloud pubsub topics list`

### "Permission denied" on SSH to jumpbox

1. Check that the SSH key path in `.env` is correct
2. Test manually: `ssh -i /path/to/key <jumpbox-user>@jumpbox hostname`
3. Check that `<jumpbox-user>` user exists on jumpbox and the key is in `~<jumpbox-user>/.ssh/authorized_keys`

### "dci-rhel-agent-ctl not found" in relay output

The jumpbox needs `dci-rhel-agent-ctl` installed. This is part of the DCI
RHEL agent package. Check:
```bash
ssh <jumpbox-user>@jumpbox "which dci-rhel-agent-ctl"
```

### "Connection refused" or timeouts on Pub/Sub

Both machines need outbound HTTPS (port 443) to `pubsub.googleapis.com`.
Test:
```bash
curl -s -o /dev/null -w "%{http_code}" https://pubsub.googleapis.com/
```
Should return `404` (the API is reachable, but the root path returns 404).

### Agent seems stuck / no progress

1. Check the relay is running: `bash container/relay.sh status`
2. Check relay logs: `bash container/relay.sh logs`
3. Check for unacknowledged messages: the relay might have crashed mid-processing
4. From Claude Code, use `dci_relay_health` to check Pub/Sub connectivity

### Relay container keeps restarting

Check logs with `bash container/relay.sh logs`. Common causes:
- Invalid GCP credentials (check `.env` paths)
- SSH key issues (wrong path or permissions)
- Git pull failure (check PAT / git credentials)

To rebuild after fixing: `bash container/relay.sh update`

---

## 14. Security Checklist

Before your first real run, verify these:

- [ ] `infra/dci-relay-sa-key.json` is NOT committed to git
- [ ] `.env` file is NOT committed to git
- [ ] Pub/Sub message retention is 10 minutes (not longer)
- [ ] The service account has ONLY `pubsub.editor` role (nothing else)
- [ ] SSH key on the Linux machine connects as `<jumpbox-user>` (not root) to the jumpbox
- [ ] The jumpbox has NO GCP credentials stored on it
- [ ] Secrets are mounted into the container, never baked into the image
- [ ] The jumpbox deploy key is read-only (it only pulls, never pushes)

---

## Quick Reference -- Configuration

### `run_config.yml` (in git -- all run settings)

| Field                       | Example                                | What it does |
|-----------------------------|----------------------------------------|-------------|
| `target`                    | `target-1.example.corp`                  | Current target server FQDN |
| `rhel_topic`                | `RHEL-9.8`                             | RHEL version being tested |
| `model`                     | `claude-opus-4-6`                      | Claude model to use |
| `max_fix_attempts`          | `5`                                    | Retries before giving up |
| `jumpbox_host`              | `jumpbox.example.corp`                  | Jumpbox hostname |
| `jumpbox_user`              | `<jumpbox-user>`                                 | SSH user on jumpbox |
| `jumpbox_repo_root`         | `/agentic-dci-workflow`            | Repo path on jumpbox |
| `disk_map`                  | `target-1: scsi-3EXAMPLE00000001`       | Per-server install disk identifiers |
| `servers`                   | (map)                                  | Known server inventory with default topics |

### Multi-server support

Per-hostname settings files are generated by `tools/configure_target.py`:

```bash
# Generate settings for a specific server
python -m tools.configure_target generate target-1 RHEL-10.2

# Discover disk for a new server (one-time)
python -m tools.configure_target discover target-5
```

The `disk_map` in `run_config.yml` maps each hostname to its install disk
identifier. Settings files are auto-synced before each workflow run.

### Operator `.env` (secrets only)

| Variable                       | Example                          | Required |
|--------------------------------|----------------------------------|----------|
| `ANTHROPIC_VERTEX_PROJECT_ID`  | `<your-vertex-project>`        | Yes      |
| `GCP_PUBSUB_PROJECT_ID`        | `<your-gcp-project>`                       | Yes      |
| `DCI_LOCAL_REPO_ROOT`          | `.`                              | No       |
| `DCI_LOG_DIR`                  | `/tmp/dci-agent-logs`            | No       |

### relay-host `.env` (secrets only)

| Variable                        | Example                          | Required |
|---------------------------------|----------------------------------|----------|
| `GCP_PUBSUB_PROJECT_ID`        | `<your-gcp-project>`                        | Yes      |
| `GOOGLE_APPLICATION_CREDENTIALS`| `/sapmnt/home/.../dci-relay-sa-key.json` | Yes |
| `JUMPBOX_SSH_KEY`              | `/sapmnt/home/.../id_ed25519`     | Yes      |

### Relay container commands

| Command                           | What it does                           |
|-----------------------------------|----------------------------------------|
| `bash container/relay.sh build`   | Build the container image              |
| `bash container/relay.sh start`   | Start the relay (detached, tails logs) |
| `bash container/relay.sh stop`    | Stop the relay                         |
| `bash container/relay.sh restart` | Stop + start                           |
| `bash container/relay.sh update`  | git pull + rebuild + restart           |
| `bash container/relay.sh logs`    | Follow container logs                  |
| `bash container/relay.sh status`  | Show if relay is running               |
| `bash container/relay.sh shell`   | Open bash inside the container         |
