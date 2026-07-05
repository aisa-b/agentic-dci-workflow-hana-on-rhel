[< Back to README](README.md)

# Getting Started: From Zero to Running Agent

This guide explains every concept as we go. No prior knowledge of AI agents,
MCP, Pub/Sub, or agentic architectures is assumed.

---

## Table of Contents

1. [What Are We Actually Building?](#1-what-are-we-actually-building)
2. [Key Concepts Explained](#2-key-concepts-explained) -- tools, MCP, agentic loop, Pub/Sub, knowledge base, skills, subagents, per-host configuration
3. [Why This Architecture and Not Another](#3-why-this-architecture-and-not-another)
4. [Stage 1: Google Cloud Setup](#4-stage-1-google-cloud-setup)
5. [Stage 2: Local Machine Setup (Agent Side)](#5-stage-2-local-machine-setup-agent-side)
6. [Stage 3: Relay Machine (Relay Side)](#6-stage-3-company-b-linux-machine-relay-side) -- now using Podman containers
7. [Stage 4: Verify Everything](#7-stage-4-verify-everything)
8. [Stage 5: First Run](#8-stage-5-first-run) -- Claude Code CLI
9. [What Happens During a Run](#9-what-happens-during-a-run)

---

## 1. What Are We Actually Building?

We are building a system where **Claude (an AI) automatically runs, diagnoses,
and fixes** a complex Ansible-based deployment pipeline (DCI) that installs
SAP HANA on bare metal servers.

The challenge: Claude runs on your local machine, but the Ansible pipeline runs
in a completely separate network (the SAP network). These two networks cannot talk
to each other directly. The only thing both can access is Google Cloud.

So we build a **bridge** through Google Cloud -- but only for the operations
that truly need the remote network (running the workflow, SSHing to the
target server). Everything else -- reading files, editing code, committing
to git -- happens **locally**, instantly.

### Two ways to run the system

**Preferred: Claude Code CLI** (interactive, with MCP integration)

```bash
cd ~/agentic-dci-workflow
claude
# Then type: /dci-run target-1
```

Claude Code CLI is the primary orchestrator. It uses native tools (file, git,
bash) plus MCP tools exposed by the `dci-relay` MCP server for remote
operations. Skills (`/dci-run`, `/dci-configure`, `/dci-fix`, `/dci-report`)
and subagents (dci-diagnostician, hana-expert, os-deploy-expert,
ansible-reviewer) provide higher-level automation on top.

**Headless mode** (for unattended/scripted runs):

```bash
claude -p "/dci-run target-1 RHEL-10.2" --allowedTools "Bash,Edit,Read,mcp__dci-relay__*"
```

### What does the system do, step by step?

1. Claude creates a git branch for its changes (locally, instant)
2. Claude checks the knowledge base for past fixes that match this scenario
3. Claude tells the jumpbox to run the DCI workflow (via relay)
4. If the workflow fails, Claude investigates (reads local files + SSHes to
   the server via relay)
5. Claude **writes a plan** explaining its diagnosis and proposed fix
6. Claude edits the Ansible playbooks locally (instant)
7. Claude commits and pushes the fix locally (instant)
8. Claude tells the jumpbox to pull the fix and re-run the workflow (via relay)
9. If it still fails, Claude evaluates whether progress was made, then tries
   again (up to 5 times)
10. After 3 failures, Claude enters **exploration mode** -- a diagnostic-only
    run to gather deep information before the final attempts
11. If it succeeds, the fix is ready for human review
12. If all 5 attempts fail, Claude reverts everything and writes a detailed
    failure report

---

## 2. Key Concepts Explained

### What are "tools" in AI?

When people talk about "tools" in the context of AI agents, they mean
**functions that the AI can call**. Claude cannot run shell commands, read
files, or make HTTP requests by itself. It can only generate text.

But if you tell Claude "here is a function called `ssh_execute` that runs
a command on a server," Claude can generate a structured request:

```json
{
  "name": "ssh_execute",
  "input": {"command": "cat /etc/redhat-release"}
}
```

Then YOUR code executes the function and sends the result back to Claude.
Claude reads the result, thinks about it, and decides what to do next.

**Tools are Claude's hands.** Without tools, Claude can only think and talk.
With tools, Claude can act on the world.

When using Claude Code CLI, the agent has access to native tools (file read,
edit, bash, git) plus **MCP tools** provided by the `dci-relay` MCP server
(configured in `.mcp.json`):

| MCP Tool | What it does | Runs where |
|----------|-------------|-----------|
| `dci_workflow_run` | Triggers the full DCI pipeline (OS deploy + SAP prep + benchmark + results) | Jumpbox (via relay) |
| `dci_workflow_status` | Polls for progress and results of a running workflow | Jumpbox (via relay) |
| `dci_workflow_stop` | Stops a specific running workflow by target hostname | Jumpbox (via relay) |
| `dci_workflow_stop_all` | Stops all running workflows | Jumpbox (via relay) |
| `dci_workflow_list` | Lists all currently running workflows | Jumpbox (via relay) |
| `dci_ssh_execute` | Runs a read-only command on the target server via SSH | Target (via relay) |
| `dci_ssh_diagnostics` | Runs a built-in diagnostic suite with a focus area hint | Target (via relay) |
| `dci_jumpbox_ping` | Checks relay/jumpbox connectivity | Jumpbox (via relay) |
| `dci_jumpbox_execute` | Runs a command on the jumpbox for process/log inspection | Jumpbox (via relay) |
| `dci_relay_update` | Pulls latest code on the relay machine and restarts the daemon | Relay machine |
| `dci_fleet_status` | Unified fleet dashboard: all workflows with phase info, alerts, nr progress | Local + relay |
| `dci_check_events` | Check for workflow completion/failure events from the background poller | Local |
| `dci_preflight_check` | Refresh Pub/Sub subscription, verify relay health, ping jumpbox | Local + relay |
| `dci_relay_health` | Shows relay infrastructure health, Pub/Sub connectivity, and stats | Relay machine |
| `dci_server_profile` | Captures and persists system profile of a target server | Target (via relay) |

Local operations (file read/edit, git commit/push, bash commands) are handled
by Claude Code's native tools -- no MCP round trip needed. This makes the
agent dramatically faster: file reads and git commits are instant instead of
taking 2-5 seconds each through Pub/Sub.

### What is the "agentic loop"?

An **agentic loop** is the cycle where:

1. You send a message to the AI
2. The AI responds with text AND/OR tool calls
3. If there are tool calls, you execute them and send results back
4. The AI reads the results and responds again (more text and/or more tool calls)
5. This repeats until the AI responds with only text (no more tool calls)

This is the core of every AI agent. The loop is what makes it "agentic" --
the AI is making decisions about what to do next based on results from
previous actions.

Claude Code CLI implements this loop internally. The `/dci-run` skill
provides the instructions that guide Claude through the workflow.

### What is MCP (Model Context Protocol)?

MCP is a **standard for connecting AI to external tools**. Think of it as
a USB standard for AI: any AI that speaks MCP can use any tool that speaks
MCP, without custom integration code.

MCP defines three things:
- **Tools**: Functions the AI can call (what we described above)
- **Resources**: Data the AI can read (files, databases, URLs)
- **Prompts**: Reusable prompt templates

**Are we using MCP?** Yes. The `dci-relay` MCP server (configured in
`.mcp.json`) exposes 15 MCP tools to Claude Code CLI. The server is
implemented in `agents/mcp_server.py` and communicates with the relay
daemon via Google Cloud Pub/Sub. Claude Code CLI starts the MCP server
automatically as a subprocess.

**Why MCP for Claude Code CLI?**
- Claude Code natively supports MCP servers -- tools appear automatically
- Enables skills (`/dci-run`, `/dci-configure`) and subagents that build
  on top of the MCP tools
- The MCP server process is lightweight -- it just wraps Pub/Sub calls
- Local tools (file read, edit, git, bash) are handled by Claude Code's
  own native tools, not MCP

### What is the difference between "agentic" and "workflow"?

From [Anthropic's research](https://anthropic.com/research/building-effective-agents):

- **Workflow**: The code decides what to do. The AI follows a predefined path.
  Example: "Step 1: summarize. Step 2: translate. Step 3: format."
- **Agent**: The AI decides what to do. It chooses which tools to call, in
  what order, based on the results it sees.

Our system is an **agent**, not a workflow. Claude decides:
- Which diagnostic commands to run
- Which files to read
- What fix to apply
- When to increase verbosity
- Whether to try a different approach on the next attempt
- When to enter exploration mode (deep diagnostics after repeated failures)

The system prompt gives Claude guidelines, but Claude makes the actual
decisions at runtime.

### What is Pub/Sub?

Pub/Sub (Publish/Subscribe) is Google Cloud's messaging service.
Think of it as a postal system:

- You write a letter (publish a message to a topic)
- Someone else picks it up (subscribes to that topic)
- The postal system handles delivery

We use two "mailboxes":
- `dci-commands`: Your machine sends commands, the relay picks them up
- `dci-results`: The relay sends results, your machine picks them up

This creates a two-way communication channel between your machine and the
remote network, even though they can't talk to each other directly.

**In our system, Pub/Sub is only used for remote operations** (workflow
management, SSH commands, diagnostics, server profiling, relay health).
Everything else (file editing, git, bash) stays local.

### What is the knowledge base?

The knowledge base is a persistent JSON file that stores past diagnoses
and fixes. Every time the agent successfully fixes a problem, it records:
- What error pattern it saw
- What it diagnosed
- What fix it applied
- Whether it worked

On future runs, the agent searches the knowledge base FIRST to see if
it has encountered a similar error before. If it has, it can skip
trial-and-error and apply a known-good fix immediately.

This means the agent gets **smarter over time** -- each run teaches it
something that helps with future runs.

### What are skills?

Skills are **slash commands** that bundle multiple tool calls and decisions
into a single high-level operation. They are invoked in Claude Code CLI
with a `/` prefix:

| Skill | What it does |
|-------|-------------|
| `/dci-run <hostname> [topic]` | Full autonomous workflow: generate settings, show for review, run, diagnose, fix, retry (up to 5 attempts). For parallel runs on multiple servers, dispatch each `/dci-run` as a separate session in `claude agents`. |
| `/dci-configure --discover <hostname>` | One-time disk discovery for new servers. SSHes to the target, identifies install disks, and saves the mapping to the `disk_map` in `run_config.yml`. |
| `/dci-configure show` | Shows the current disk_map and server status. |
| `/dci-fix <error>` | Applies a single targeted fix based on a known error pattern from the knowledge base. |
| `/dci-report` | Generates a failure report PR and reverts all agent changes. |

Skills are invoked via `/skill-name` in the Claude Code prompt.

### What are subagents?

Subagents are **specialized Claude instances** that can be delegated to
for specific domains. They have their own system prompts and expertise.
Invoke them by saying "use the X subagent" in Claude Code CLI:

| Subagent | Specialization |
|----------|---------------|
| `dci-diagnostician` | Exhaustive read-only diagnosis of failures from an SRE perspective. Investigates logs, server state, and Ansible output without making changes. |
| `hana-expert` | SAP HANA installation and runtime health assessment. Checks HANA-specific configurations, memory, storage, and tuned profiles on the target server. |
| `os-deploy-expert` | Phase 1 OS deployment specialist. Handles kickstart, PXE, partitioning, BIOS settings, and BMC/iLO issues. |
| `ansible-reviewer` | Reviews Ansible changes for correctness before committing. Checks for syntax, idempotency, and side effects. |

Subagents are available in Claude Code CLI via the Agent tool.

### What is per-host configuration?

The system supports **8 target servers**: target-1, target-2, target-3, target-4, target-5,
target-6, target-7, target-8. Each server has different hardware (disk layout,
memory, CPU) and requires its own settings.

The `disk_map` in `run_config.yml` maps each hostname to its install disk
identifiers (SCSI IDs or device names). This is populated once per server
using `/dci-configure --discover <hostname>`.

The `tools/configure_target.py` script generates per-hostname settings files
(`settings/settings_current_<hostname>.yml`) from the disk_map and server
profiles. Multiple settings files can coexist for parallel runs on different
servers.

Every call to `dci_workflow_run()` automatically regenerates the settings
file, commits and pushes if it changed, and the relay deploys it before
starting the workflow.

---

## 3. Why This Architecture and Not Another

### Why run most tools locally?

In an earlier version, ALL operations went through the relay via Pub/Sub.
Every file read, every git commit, every grep -- all of it required a
round trip through Google Cloud. This had three problems:

1. **Speed**: Each Pub/Sub round trip added 2-5 seconds of latency. A single
   diagnosis cycle (read 3 files, search 2 patterns, edit 1 file, commit)
   took 20-30 seconds in network overhead alone. Now it's instant.

2. **Reliability**: More messages through Pub/Sub means more chances for
   timeouts, message loss, or ordering issues. By keeping file and git
   operations local, the system is much more reliable.

3. **Attack surface**: Every message through Pub/Sub carries potentially
   sensitive data (file contents, git diffs, Ansible output). The fewer
   messages that transit Google Cloud, the smaller the attack surface.
   Now only workflow triggers and SSH diagnostics go through the bridge.

The files live in a git repo. Your machine has a clone. The jumpbox has a
clone. They stay in sync through `git push` (local) and `git pull`
(relay-side, triggered automatically before each workflow run). There is
no need for the relay to read/write files on behalf of the agent.

### Why not run Claude directly on the jumpbox?

The jumpbox is a shared machine in the SAP network. Installing Claude
(which requires Google Cloud credentials, API access, and Python packages)
on a shared machine is a security risk. Other people use the jumpbox.

By running Claude on your own machine, all AI-related credentials stay on a
machine you control.

### Why not use a VPN or direct SSH?

Your machine is on the operator network. The jumpbox is on the remote network.
There is no VPN between them. Only HTTPS outbound is available from both sides.

The only reliable connection both networks share is Google Cloud (HTTPS
outbound). Pub/Sub uses HTTPS, so it works from both sides.

### Why not use Claude Agent SDK or Google ADK?

We evaluated three frameworks:

| Framework | Why we didn't use it |
|-----------|---------------------|
| **Google ADK** | The original implementation used this. It required SequentialAgent/LoopAgent/BaseAgent hierarchy -- too rigid. Claude can handle the workflow in a single context without forced structure. |
| **Claude Agent SDK** | Good, but wraps Claude Code CLI under the hood. Adds a process layer we don't need. Our tools are remote (Pub/Sub), not local. |
| **LangChain / CrewAI** | Heavy frameworks that add abstraction without value for our use case. Anthropic's own recommendation: "Start with LLM APIs directly." |

We use the **Anthropic Python SDK directly** (`anthropic.AnthropicVertex`).
The agentic loop is ~80 lines. No framework. This follows Anthropic's
principle: "The most successful implementations use simple, composable
patterns rather than complex frameworks."

### Why MCP for remote tools but not local tools?

Remote tools (workflow, SSH, diagnostics) are exposed via the `dci-relay`
MCP server because Claude Code CLI natively discovers and calls MCP tools.
Local tools (file read, edit, git, bash) use Claude Code's own native
capabilities -- wrapping them in MCP would add overhead without benefit.

---

## 4. Stage 1: Google Cloud Setup

**What we're doing:** Creating the "postal system" (Pub/Sub) that lets
your local machine and the remote network exchange messages through Google Cloud.

**Why:** Your machine and the jumpbox can't talk directly. Pub/Sub is the
bridge -- both sides can send HTTPS requests to Google Cloud. But we only
use this bridge for 3 operations (workflow runs and SSH diagnostics).
Everything else runs locally.

**Run all commands on: Your local machine**

### 4.1. Install and authenticate the Google Cloud CLI

```bash
# Install gcloud (if not already installed)
# See: https://cloud.google.com/sdk/docs/install

# Log in with your Google account
gcloud auth login
```

**What this does:** Opens a browser. You log in with the Google account
that has access to your GCP project. After this, the `gcloud` command
can manage your Google Cloud resources.

### 4.2. Set your project and enable Pub/Sub

```bash
export GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"
gcloud config set project "$GCP_PUBSUB_PROJECT_ID"
gcloud services enable pubsub.googleapis.com
```

**What this does:**
- `config set project` tells gcloud which project to work with
- `services enable` turns on the Pub/Sub API for that project (one-time)

### 4.3. Create the two topics (mailboxes)

```bash
gcloud pubsub topics create dci-commands \
    --message-retention-duration=600s

gcloud pubsub topics create dci-results \
    --message-retention-duration=600s
```

**What this does:** Creates two named message channels.
- `dci-commands` is where your machine will send orders
- `dci-results` is where the relay will send answers
- `--message-retention-duration=600s` means messages are deleted after
  10 minutes. This prevents sensitive data (Ansible logs, server info)
  from sitting in Google Cloud longer than needed.

### 4.4. Create the subscriptions (listeners)

```bash
# The relay listens for commands
gcloud pubsub subscriptions create dci-commands-relay-sub \
    --topic=dci-commands \
    --ack-deadline=600 \
    --message-retention-duration=600s \
    --expiration-period=never

# Your machine listens for results
gcloud pubsub subscriptions create dci-results-agent-sub \
    --topic=dci-results \
    --ack-deadline=120 \
    --message-retention-duration=600s \
    --expiration-period=never
```

**What this does:** Creates listeners attached to the topics.
- `--ack-deadline=600` means the relay has 10 minutes to process a command
  before Pub/Sub assumes it failed and re-delivers it. We set this high
  because DCI workflows can take hours.
- `--expiration-period=never` means the subscription doesn't expire from
  inactivity. Without this, Google deletes unused subscriptions after 31 days.

### 4.5. Create a service account (robot user)

```bash
gcloud iam service-accounts create dci-agent-relay \
    --display-name="DCI Agent Relay Service Account"

SA_EMAIL="dci-agent-relay@${GCP_PUBSUB_PROJECT_ID}.iam.gserviceaccount.com"

# Grant it permission to publish messages
gcloud projects add-iam-policy-binding "$GCP_PUBSUB_PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/pubsub.publisher" \
    --condition=None --quiet

# Grant it permission to subscribe and receive messages
gcloud projects add-iam-policy-binding "$GCP_PUBSUB_PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/pubsub.subscriber" \
    --condition=None --quiet
```

**What this does:** Creates a "robot user" that your code uses to
authenticate with Google Cloud. Unlike your personal account, a service
account has a downloadable key file that code can use without a browser.

We give it ONLY two permissions: publish and subscribe to Pub/Sub.
It cannot access any other Google Cloud service. This is the principle
of **least privilege** -- if the key leaks, the damage is limited to
Pub/Sub messaging only.

### 4.6. Download the key

```bash
mkdir -p infra
gcloud iam service-accounts keys create infra/dci-relay-sa-key.json \
    --iam-account="$SA_EMAIL"
```

**What this does:** Downloads a JSON file containing the service account
credentials. This file is like a password -- keep it safe, never commit
it to git. You'll copy it to the relay machine later.

---

## 5. Stage 2: Local Machine Setup (Agent Side)

**What we're doing:** Setting up Claude Code CLI and the Python environment
on your local machine where Claude will run and make decisions.

**Why:** Your machine is the "brain" AND the "hands" of the system. Claude
runs here, reads and edits files here, commits to git here, makes
decisions here. Only remote operations (workflow runs, SSH diagnostics,
relay management) require the Pub/Sub bridge.

### 5.1. Clone the repository

```bash
git clone https://github.com/aisa-b/agentic-dci-workflow.git ~/agentic-dci-workflow
cd ~/agentic-dci-workflow
```

### 5.2. Install Claude Code CLI

```bash
# Install Claude Code CLI (requires Node.js)
npm install -g @anthropic-ai/claude-code
```

Claude Code CLI is the primary way to interact with the system. It
automatically discovers the MCP tools defined in `.mcp.json` and makes
skills and subagents available.

### 5.3. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**What this does:** Creates an isolated Python environment so the project's
dependencies don't interfere with your system Python. The `source activate`
command switches your terminal into this environment.

**Why a virtual environment?** If you install packages globally, they can
conflict with other projects. Virtual environments keep everything isolated.

### 5.4. Install dependencies

```bash
pip install -r requirements.txt
```

**What this installs:**
- `anthropic[vertex]` -- The Anthropic Python SDK with Vertex AI support.
  This is how our code talks to Claude's API. The `[vertex]` extra adds
  Google Vertex AI authentication (Claude runs on Vertex in our setup).
- `google-cloud-pubsub` -- The Pub/Sub client library. This is how the
  MCP server communicates with the relay for remote operations.
- `python-dotenv` -- Loads environment variables from a `.env` file so
  you don't have to export them manually.

### 5.5. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your actual values. The most important ones:

```bash
# How to authenticate with Claude (via Vertex AI)
ANTHROPIC_VERTEX_PROJECT_ID="<your-vertex-project>"

# Pub/Sub project (separate from Vertex AI)
GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"
```

All other settings (target server, model, jumpbox paths) are in
`run_config.yml` -- the single source of truth for run settings.

**What these mean:**
- `ANTHROPIC_VERTEX_PROJECT_ID` is your GCP project that has access to
  Claude. Claude runs on Google's Vertex AI, not on Anthropic's servers
  directly. This is how enterprises use Claude.
- `GCP_PUBSUB_PROJECT_ID` is the GCP project for Pub/Sub messaging.
  It is separate from the Vertex AI project for security.

### 5.6. Authenticate with Vertex AI

```bash
gcloud auth application-default login
```

**What this does:** Creates application-default credentials that the
Anthropic SDK uses to authenticate with Vertex AI (where Claude runs).
This is a separate credential from the service account -- it uses your
personal Google account.

---

## 6. Stage 3: Relay Machine (Relay Side)

**What we're doing:** Setting up the relay daemon -- a small service that
sits on the remote network. It handles remote operations: running
workflows, executing SSH commands, gathering diagnostics, managing server
profiles, and providing health/status information. No file editing, no git
commands -- those all happen locally.

**Why:** The relay is just the "remote hands" for operations that
physically require the remote network. It can SSH to the jumpbox and
the target server. Your machine cannot.

The relay now runs in a **Podman container** for reproducibility and
isolation. The container files are in `container/`:
- `container/Containerfile.relay` -- Container image definition
- `container/relay.sh` -- Start/stop/restart the relay container
- `container/entrypoint.sh` -- Container entrypoint script

### 6.1. Get the code on the machine

```bash
git clone https://github.com/aisa-b/agentic-dci-workflow.git ~/dci-agent
cd ~/dci-agent
```

### 6.2. Copy the GCP service account key

From your local machine:

```bash
scp infra/dci-relay-sa-key.json user@linux-machine:~/dci-agent/
```

### 6.3. Create the `.env` file

The relay container reads secrets from `~/dci-agent/.env` at startup.
Create it **before** building the container:

```bash
cat > ~/dci-agent/.env << 'EOF'
GCP_PUBSUB_PROJECT_ID="<your-gcp-project>"
PUBSUB_COMMANDS_SUB="dci-commands-relay-sub"
PUBSUB_RESULTS_TOPIC="dci-results"
GOOGLE_APPLICATION_CREDENTIALS="/home/you/dci-agent/dci-relay-sa-key.json"

JUMPBOX_HOST="jumpbox"
JUMPBOX_USER="<jumpbox-user>"
JUMPBOX_SSH_KEY="/home/you/.ssh/id_rsa"
EOF
```

Replace the placeholder values with your actual paths and credentials.

Note: Target host, settings file, and repo root are now configured in
`run_config.yml` (not `.env`). The relay reads `run_config.yml` from the
git repo and reloads it after each `git pull`.

The system supports multiple target servers. Currently configured servers:
target-1, target-2, target-3, target-4, target-5, target-6, target-7, target-8. Each has a
per-hostname settings file generated by `tools/configure_target.py`.

### 6.4. Copy `run_config.yml`

`run_config.yml` is gitignored (it contains environment-specific config), so
it won't be in the clone. Copy it from your local machine:

```bash
scp run_config.yml user@linux-machine:~/dci-agent/run_config.yml
```

The relay reads this file for jumpbox settings, server FQDNs, disk maps, and
Pub/Sub topic names. Without it the relay falls back to `.env` only and
won't have disk maps for settings file generation.

### 6.5. Configure Podman storage (if home is on NFS)

If your home directory is on a network filesystem (NFS/CIFS), Podman's
overlay storage won't work. Move it to a local filesystem:

```bash
sudo mkdir -p /var/tmp/<your-user>-containers/storage /var/tmp/<your-user>-containers/run
sudo chown -R $(id -u) /var/tmp/<your-user>-containers
mkdir -p ~/.config/containers
cat > ~/.config/containers/storage.conf << 'SEOF'
[storage]
driver = "overlay"
graphroot = "/var/tmp/<your-user>-containers/storage"
runroot = "/var/tmp/<your-user>-containers/run"
SEOF
```

Skip this step if your home is on a local filesystem.

### 6.6. Run preflight checks

Before starting the relay, verify all prerequisites:

```bash
bash container/relay.sh preflight
```

This checks: container runtime, `.env` values, GCP SA key, SSH key,
`run_config.yml`, log directory permissions, container image, Podman
storage health, DNS resolution, and jumpbox SSH connectivity. Fix any
errors before proceeding.

### 6.7. Build and start the relay container

```bash
bash container/relay.sh start
```

This runs cleanup, preflight checks, builds the container image (if
needed), and starts the relay daemon.

Other commands:

| Command | Description |
|---|---|
| `bash container/relay.sh preflight` | Check all prerequisites |
| `bash container/relay.sh clean` | Kill hanging processes, fix permissions |
| `bash container/relay.sh start` | Clean + preflight + start (detached) |
| `bash container/relay.sh start-fg` | Start in foreground (for debugging) |
| `bash container/relay.sh stop` | Stop the relay |
| `bash container/relay.sh restart` | Stop + start |
| `bash container/relay.sh update` | git pull + rebuild + restart |
| `bash container/relay.sh status` | Show if relay is running |
| `bash container/relay.sh logs` | Follow container logs |
| `bash container/relay.sh shell` | Open bash inside the container |

**If the container crashes:** Run `bash container/relay.sh start-fg` to see
the full error output. The crash diagnostics include exit code, SELinux
denials, file permissions, disk space, and OOM checks.

**Alternative (without container):** You can still run directly in a Python
venv if needed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r relay-requirements.txt
python3 -m relay.daemon
```

### 6.8. Enable the systemd service (optional, recommended)

To keep the relay running across machine reboots:

```bash
mkdir -p ~/.config/systemd/user
cp container/dci-relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dci-relay.service
```

The service uses `Restart=on-failure` with a burst limit (5 restarts in
60 seconds). If the relay keeps crashing, it stops trying and waits for
manual intervention:

```bash
systemctl --user status dci-relay.service    # Check status
journalctl --user -u dci-relay.service -f    # Follow logs
systemctl --user restart dci-relay.service   # Manual restart
```

### 6.9. Test SSH connectivity

Test relay → jumpbox:

```bash
ssh <jumpbox-user>@<jumpbox-fqdn> "hostname && echo OK"
```

Test relay → jumpbox → target (two-hop):

```bash
ssh <jumpbox-user>@<jumpbox-fqdn> "ssh root@<target-fqdn> hostname"
```

If the first command fails, fix SSH access to the jumpbox before proceeding.
If the second fails, check that the jumpbox can reach the target server.

---

## 7. Stage 4: Verify Everything

**What we're doing:** Testing each layer of the system independently
before running the full agent. This catches configuration errors early.

### 7.1. Test Pub/Sub (Local Machine -> Linux Machine)

**On your local machine:**

```bash
source .venv/bin/activate
python3 -c "
from google.cloud import pubsub_v1
import json, os

publisher = pubsub_v1.PublisherClient()
topic = f'projects/{os.environ[\"GCP_PUBSUB_PROJECT_ID\"]}/topics/dci-commands'
data = json.dumps({'test': True}).encode()
future = publisher.publish(topic, data)
print(f'Published: {future.result()}')
"
```

**On the relay machine** (inside the running container):

```bash
podman exec dci-relay python3 -c "
from google.cloud import pubsub_v1
import json, os

subscriber = pubsub_v1.SubscriberClient()
sub = f'projects/{os.environ[\"GCP_PUBSUB_PROJECT_ID\"]}/subscriptions/dci-commands-relay-sub'
response = subscriber.pull(subscription=sub, max_messages=1, timeout=10)
for msg in response.received_messages:
    print(f'Received: {json.loads(msg.message.data)}')
    subscriber.acknowledge(subscription=sub, ack_ids=[msg.ack_id])
"
```

**What this tests:** The full Pub/Sub path. Your machine publishes a message
to Google Cloud. The relay container picks it up. If this works, the bridge
is functional.

### 7.2. Test SSH from relay to jumpbox

**On the relay machine:**

```bash
ssh <jumpbox-user>@jumpbox "hostname && echo OK"
```

And the two-hop path to a target server:

```bash
ssh <jumpbox-user>@jumpbox "ssh root@<target-fqdn> hostname"
```

**What this tests:** The relay's ability to reach the jumpbox and target
server via SSH. This is how the relay triggers `dci-rhel-agent-ctl`.

### 7.3. Test Claude access

**On your local machine:**

```bash
python3 -c "
import anthropic
client = anthropic.AnthropicVertex(
    project_id='<your-vertex-project>',
    region='global',
)
response = client.messages.create(
    model='claude-opus-4-7',
    max_tokens=100,
    messages=[{'role': 'user', 'content': 'Say hello in one word.'}],
)
print(response.content[0].text)
"
```

**What this tests:** That your machine can reach Claude via Vertex AI.
If this fails, check your `gcloud auth application-default login`.

---

## 8. Stage 5: First Run

### 8.1. Start the relay daemon

If you enabled the systemd service in step 6.8, the relay is already
running. Check with:

```bash
systemctl --user status dci-relay.service
```

Otherwise, start it manually **on the relay machine:**

```bash
cd ~/dci-agent
bash container/relay.sh start
```

Check status with `bash container/relay.sh status`.

Leave it running.

### 8.2. Start the agent

**On your local machine (preferred -- Claude Code CLI):**

```bash
cd ~/agentic-dci-workflow
claude
```

Claude Code CLI starts with access to all MCP tools, skills, and subagents.
To start a full autonomous workflow:

```
> /dci-run target-1
```

This will generate the settings file, show it for review, run the workflow,
diagnose failures, apply fixes, and retry -- all autonomously.

**Headless (unattended):**

```bash
claude -p "/dci-run target-1 RHEL-10.2" --allowedTools "Bash,Edit,Read,mcp__dci-relay__*"
```

---

## 9. What Happens During a Run

Here is the complete flow, annotated:

```
STEP 1: CREATE BRANCH (local, instant)
  Claude calls: create_fix_branch()
  -> Runs locally: git checkout -b agent-fix/20260508
  -> Returns instantly: {branch: "agent-fix/20260508"}

STEP 2: CHECK KNOWLEDGE BASE (local, instant)
  Claude calls: search_knowledge("sap_prepare failure")
  -> Searches the local knowledge_base.json file
  -> Returns any past fixes that match the error pattern

STEP 3: RUN WORKFLOW (remote, ~2 hours)
  Claude calls: run_dci_workflow(verbosity=0)
  -> Publishes to Pub/Sub: {command: "workflow.run"}
  -> Relay receives, SSHes to jumpbox: git pull && dci-rhel-agent-ctl ...
  -> (This takes ~2 hours)
  -> Relay publishes result: {success: false, failures: [...]}
  -> Result received, fed to Claude

STEP 4: DIAGNOSE (local + remote)
  Claude reads local files (instant):
    read_file("dude/workload/saphana/setup.yml")
    search_files("sap-preconfigure", "*.yml")
    -> Runs directly on local filesystem, no relay needed

  Claude SSHes to target (remote, via relay):
    gather_diagnostics("sap_prepare")
    ssh_execute("ausearch -m avc --start recent")
    -> Relay SSHes to jumpbox -> target, returns diagnostic data

STEP 5: PLAN (Claude writes its reasoning)
  Before touching any file, Claude writes out a plan:
    "PLAN:
     Root cause: SELinux denying access to /opt/sap during sap-preconfigure role
     Evidence: ausearch shows 3 AVC denials for sapinst_t context
     Proposed fix: Add SELinux boolean sap_preconfigure_all to pre-task
     Confidence: High -- exact same denial pattern
     Fallback: Try permissive mode for the specific domain
     Risk: Low -- only adds a boolean, doesn't disable SELinux"

STEP 6: FIX (local, instant)
  Claude edits the file locally:
    edit_file("dude/workload/saphana/setup.yml", original="...", replacement="...")
    -> Instant -- directly writes to local filesystem
    -> No-delete policy enforced locally (lines must be commented, not removed)

STEP 7: COMMIT AND PUSH (local, fast)
  Claude commits and pushes:
    git_commit(message="Enable SELinux boolean for SAP preconfigure",
               files=["dude/workload/saphana/setup.yml"])
    git_push()
    push_and_create_pr(title="[DCI Agent] Fix SELinux denial in sap-preconfigure",
                       body="...")
    -> All local git commands, push goes directly to GitHub

STEP 8: RE-RUN (remote, the relay does git pull automatically)
  Claude calls: run_dci_workflow(verbosity=2)
  -> Relay SSHes to jumpbox: git pull && dci-rhel-agent-ctl ...
  -> The jumpbox gets the latest code (including Claude's fix) via git pull
  -> If success: DONE -> go to Step 9
  -> If failure: Claude evaluates progress...

  PROGRESS DETECTION:
    Claude compares the new failure to the old one:
    - Failed in a LATER phase? Fix worked, new issue revealed. Progress!
    - Same failure? Fix didn't work. Try a different approach.
    - Failed in an EARLIER phase? Fix may have broken something. Consider revert.

  Back to Step 4 (up to 5 times)

  EXPLORATION MODE (after 3 failed fixes):
    Instead of immediately trying fix #4, Claude pauses:
    - Does NOT edit any files
    - Runs the workflow with maximum verbosity (verbosity=4)
    - Runs extensive diagnostics across multiple areas
    - SSHes to target for deep inspection (dmesg, journalctl, package versions)
    - Writes a comprehensive analysis
    - THEN decides: is this fixable, or does it need a human?
    - If fixable: applies fix #4 with high confidence
    - If not fixable: goes straight to failure report

STEP 9: FINALIZE
  On success:
    Claude calls record_fix() to save this fix to the knowledge base.
    The PR already exists from Step 7. Done!

  On failure (all 5 attempts exhausted):
    Claude calls revert_all_fixes() -- each commit gets a revert commit
    Claude calls git_push() to push the reverts
    Claude calls push_and_create_pr() with a detailed failure report
    covering every attempt, every diagnosis, and recommendations
```

### How long does a run take?

- Each workflow run: ~2 hours (a successful run takes approximately 120 minutes)
- Diagnostics: 10-30 seconds per SSH command via relay
- Local file reads/edits: **instant** (milliseconds, no network)
- Local git operations: **instant** (milliseconds, push is a few seconds)
- Total for a 5-attempt run: Could be ~10 hours

### How much does it cost?

- Claude API calls (Opus): ~$5-15 per run (depending on how many tool calls)
- Pub/Sub: FREE. 10 GiB/month free tier. We use ~360 KB per run (~29,000
  runs/month before any charges). A built-in usage tracker hard-blocks
  publishing at 95% to guarantee you never pay.
- The main cost is time, not money

---

## Quick Architecture Diagram

```
YOUR MACHINE (brain + local tools)            GOOGLE CLOUD          COMPANY B (remote only)
+-------------------------------+           +----------+          +---------------------+
| Claude Code CLI               |           |          |          |                     |
|                               |           |  Pub/Sub |          |  Relay daemon       |
|                               |--cmd----->|  topics  |<--sub---| (Podman container)  |
| LOCAL (instant, native):      |<--sub-----|          |---pub--->|                     |
|  - file read/edit             |           +----------+          | MCP tools:       |
|  - bash commands              |                                 |  workflow.run/stop  |
|  - git commit/push/diff       |                                 |  workflow.status    |
|  - search/grep                |                                 |  workflow.list      |
|                               |                                 |  ssh.execute        |
| MCP tools (via relay):     |                                 |  ssh.diagnostics    |
|  - dci_workflow_run           |                                 |  jumpbox.ping       |
|  - dci_workflow_status        |                                 |  jumpbox.execute    |
|  - dci_workflow_stop[_all]    |                                 |  relay.update       |
|  - dci_workflow_list          |                                 |  relay.health       |
|  - dci_ssh_execute            |                                 |  server.profile     |
|  - dci_ssh_diagnostics        |                                 |                     |
|  - dci_jumpbox_ping           |                                 | SSH to:             |
|  - dci_jumpbox_execute        |                                 |  jumpbox (jumpbox)   |
|  - dci_relay_update           |                                 |  7 target servers   |
|  - dci_relay_health           |                                 +---------------------+
|  - dci_server_profile         |                                       |
|                               |                                  Jumpbox:
| Skills:                       |                                  - git pull
|  /dci-run, /dci-configure     |                                  - dci-rhel-agent-ctl
|  /dci-fix, /dci-report        |                                  - SSH to targets
|                               |
| Subagents:                    |
|  dci-diagnostician            |
|  hana-expert                  |
|  os-deploy-expert             |
|  ansible-reviewer             |
|                               |
| Knowledge base (JSON)         |
+-------------------------------+
```

**Key insight:** The operator machine does everything except what physically
requires the remote network. File operations are instant (local filesystem).
Git operations are instant (local git, push to GitHub). Only running the
Ansible workflow, SSHing to target servers, and managing the relay require
the Pub/Sub bridge.

---

**Next:** [Runbook](RUNBOOK.md) — operations, monitoring, troubleshooting
