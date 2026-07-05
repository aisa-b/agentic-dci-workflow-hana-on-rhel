# Autonomous SAP HANA Benchmarking with AI Agents

## The Problem

We run SAP HANA performance benchmarks on bare-metal servers as part of
the DCI (Distributed CI) pipeline. Each run involves deploying a fresh
RHEL installation, configuring the OS for SAP, installing HANA, running
the PBOffline benchmark, and collecting results. The pipeline is a
5-phase Ansible workflow that takes approximately 2 hours per run and
breaks frequently — RHEL minor version upgrades introduce package
mismatches, SELinux policy changes, tuned profile regressions, disk
mapping differences across hardware vendors (HPE vs Lenovo), and dozens
of other failure modes that vary by server and OS version.

Historically, when the pipeline broke, a human engineer would SSH into
the server, read the Ansible output, cross-reference documentation,
identify the root cause, write a fix, push it, and re-run. This cycle
could consume an entire day for a single failure, and the knowledge
gained was trapped in the engineer's head.

## The Constraint: Network Isolation

The SAP lab network is reachable only through Citrix — there is no VPN
access. An AI agent running on a developer's machine cannot SSH directly
into the lab. This rules out the straightforward approach of giving an
LLM direct shell access to the infrastructure. We needed a message-based
architecture that bridges two isolated networks without requiring either
side to expose endpoints to the other.

## The Solution: An AI Agent with a Pub/Sub Bridge

The system uses Claude Opus 4.6 as an autonomous agent that can diagnose,
plan, fix, and re-run the entire DCI pipeline without human
intervention. Google Cloud Pub/Sub acts as the message bridge between the
developer's machine (Company A network) and the SAP lab (Company B
network). Both sides can reach Google Cloud via HTTPS — neither needs to
accept inbound connections.

### Architecture

Five machines participate in each run:

1. **Operator machine** (Mac) — runs Claude Code CLI with MCP tools.
   The agent reads Ansible output, analyzes failures, edits playbooks,
   commits fixes, and dispatches re-runs. All file and git operations
   are local.

2. **Google Cloud Pub/Sub** — the message bridge. Commands flow from
   the operator to a `dci-commands` topic; results flow back via a
   `dci-results` topic. Sub-second latency, 10 MB message limit, no
   public endpoints required on either side.

3. **Relay machine** (Company B Linux VM) — a lightweight daemon in a
   Podman container that translates Pub/Sub messages into SSH commands.
   It has no AI capabilities. It receives an instruction ("run this
   workflow", "execute this SSH command"), executes it on the jumpbox
   or target server, and publishes the result. The relay is the only
   component with credentials for both Google Cloud and the SAP
   network.

4. **Jumpbox** (Company B) — the Ansible controller. Runs
   `dci-rhel-agent-ctl` with hooks that define the 5-phase workflow.
   The relay does `git pull` here before each run so code changes
   propagate automatically.

5. **Target servers** (bare metal) — HPE and Lenovo SAP HANA servers.
   Each run deploys a fresh OS, so SSH host keys change every time.
   Multiple targets can run in parallel via independent Claude Code
   sessions.

### How It Works

A typical autonomous run:

1. The operator types `/dci-run <hostname> RHEL-9.8` in Claude Code.
2. The agent generates a per-server settings file, commits it, pushes
   to GitHub, and dispatches the workflow via Pub/Sub.
3. The relay receives the command, pulls the latest code on the
   jumpbox, and starts `dci-rhel-agent-ctl`.
4. The agent monitors progress via heartbeat messages. If the workflow
   succeeds (all 5 phases pass), it records the result and reports.
5. If the workflow fails, the agent enters a diagnosis-fix-retry loop:
   - **Triage:** searches a local knowledge base for prior fixes,
     analyzes the Ansible output, and delegates to specialized
     subagents (OS deployment expert, HANA expert, diagnostician) for
     SSH-based investigation.
   - **Plan:** writes a structured plan with root cause, evidence,
     proposed fix, confidence level, and fallback.
   - **Fix:** edits the playbook, has an Ansible reviewer subagent
     validate the change, commits, pushes, and triggers a full re-run.
   - **Evaluate:** compares the new failure (if any) against the
     previous one. If progress was made (failure moved to a later
     phase), the fix is kept. If not, it is reverted.
   - This loop runs up to 5 times. After 3 consecutive failures, the
     agent enters an "exploration mode" with maximum diagnostic
     verbosity before attempting another fix.

### How It Learns

Every fix — successful or not — is recorded in a unified knowledge base
(`knowledge_base.json`) with the error pattern, diagnosis, fix applied,
server state, and outcome. Entries are tagged by domain (OS deployment,
SAP configuration, HANA installation, benchmark execution) so all four
subagents share a single store rather than maintaining separate files.
On subsequent runs, the agent checks this knowledge base before doing
fresh diagnosis. Fixes that worked on one server generalize to others
with similar hardware. The knowledge base currently contains patterns
accumulated across hundreds of runs on 8 different servers.

## Keeping AI Safe on the SAP Network

The agent has significant autonomy, but every action it can take is
structurally constrained. Safety controls are split into two categories:

**Advisory controls** (operator-side, prompt-enforced):

The LLM cooperates with these by design, but they are not enforced by
code at the relay level.

- **No-delete invariant** -- the agent comments out code instead of
  deleting it. Every change is a new git commit on a branch, reviewed
  by a subagent before pushing, and reverted if it does not improve
  the outcome.
- **Git branch isolation** -- the agent works on `agent-fix/*`
  branches and never touches main directly.

**Structural controls** (relay-side, code-enforced):

These are enforced at the relay regardless of what the LLM decides.
Even if the AI is manipulated via prompt injection in server output,
these controls prevent dangerous actions.

1. **The agent never touches the SAP network directly.** All remote
   operations go through the relay, which enforces its own safety
   checks independently of the AI.

2. **Destruction blocklist** (37 patterns) -- commands like `rm -rf`,
   `mkfs`, `reboot`, `dd`, `iptables -F` are hard-blocked at the
   relay level. The agent cannot execute them regardless of what it
   decides to do.

3. **SSH allowlists** -- the target server accepts only 47 read-only
   command prefixes (`cat`, `systemctl status`, `rpm -qa`, `df`, etc.).
   The jumpbox has its own 42-prefix allowlist. Anything not on the
   list is rejected.

4. **Path restrictions** -- the relay only allows commands that
   reference the project's own repository directory. Attempts to
   access other paths are blocked.

5. **Regex injection detection** -- subshells, `eval`, backticks, and
   other shell injection patterns are caught and rejected before
   execution.

6. **Secret scrubbing** -- passwords, tokens, and credentials are
   stripped from output before Pub/Sub transit.

7. **Output wrapping** -- all remote output is wrapped in markers
   (`BEGIN REMOTE OUTPUT` / `END REMOTE OUTPUT`) so the agent treats
   it as data, not instructions. This is a defense against prompt
   injection via server output.

**External gate:**

- **Credential isolation** -- the GCP project for Pub/Sub messaging
  is separate from the GCP project for Claude/Vertex AI. The agent
  cannot escalate from one to the other. Credentials are in `.env`
  files that are not in git.

- **Human gate** -- every fix creates a PR on GitHub. Even after the
  agent's autonomous loop completes, a human reviews and merges (or
  reverts) the changes.

These layers are independent — bypassing one does not weaken the
others.

## What's Next

The current system handles deployment failures, OS configuration
issues, and benchmark execution problems. The next phase will extend
it to **performance analysis**:

- Integrating with existing performance monitoring tools to detect
  regressions in HANA benchmark scores across runs.
- Correlating performance metrics with OS configuration changes
  (kernel parameters, tuned profiles, NUMA topology) to identify
  which changes caused regressions.
- Building a feedback loop where the agent not only fixes broken
  pipelines but also investigates *why* a benchmark score dropped
  and proposes configuration improvements.

The goal is to move from "make the pipeline pass" to "make the
pipeline pass *with optimal performance*."

## Open Question

Is anyone else working on a similar project — using AI agents to
autonomously diagnose and fix issues in CI/CD workflows or
infrastructure pipelines? We would be interested in comparing
approaches, especially around safety mechanisms for AI operating on
production-adjacent infrastructure.
