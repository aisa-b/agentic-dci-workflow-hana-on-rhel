---
name: dci-fix
description: Apply a single targeted fix to DCI hooks based on a known error
disable-model-invocation: true
---

# DCI Single Fix

Apply one targeted fix to the DCI hooks codebase based on a known error. This skill
does NOT re-run the workflow — use `/dci-run` for the full autonomous loop.

## Input

`$ARGUMENTS` contains the error description or message to fix. Examples:
- `"TASK [sap-preconfigure] fatal: package sap-hana-client not found"`
- `"SELinux denial on /hana/shared mount"`
- `"tuned profile sap-hana not available on RHEL 10"`

## Safety Rules

- **NEVER delete code.** Comment out with `#` prefix + `# [AGENT-DISABLED]` marker.
- **New code gets `# [AGENT-ADDED]` marker.**
- **NEVER access `banned-host` or `/banned/path/`.**
- **One fix per invocation.**
- **Git push ONLY to** `origin` (`github.com/aisa-b/agentic-dci-workflow`).
- **NEVER add `Co-authored-by` to commit messages.**

## Step 1: Understand the Error

1. Parse `$ARGUMENTS` for the error message, task name, file path, and phase.
2. Read `run_config.yml` for current target, RHEL topic, and jumpbox paths.
3. Check the knowledge base:

```bash
cat agents/local/knowledge_base.json 2>/dev/null || echo "No knowledge base yet."
```

Search for prior fixes matching this error pattern.

## Step 2: Locate the Problem

1. Identify which file in `dci-hooks/` is involved.
   Common locations by phase:
   - **OS Deployment:** `dude/deploytype/`, `repos/`, `pre-run.yml`
   - **SAP Prep:** `dude/workload/saphana/`, `config-variables.yml`
   - **Benchmark:** `dude/benchmark/pbo/`
   - **Results:** `reporting/`

2. Read the relevant file(s).

3. Investigate the target server and jumpbox using MCP tools:

   **Target server** (where HANA/PBO run) via `dci_ssh_execute`:
   ```
   dci_ssh_execute("cat /etc/redhat-release")
   dci_ssh_execute("getenforce")
   dci_ssh_execute("tuned-adm active")
   dci_ssh_execute("journalctl -p err --no-pager -n 50")
   dci_ssh_execute("dmesg | tail -30")
   dci_ssh_execute("ps aux | grep -E 'sap|hana|pbo'")
   dci_ssh_execute("rpm -qa | grep <suspect-package>")
   ```

   **Jumpbox** (where Ansible/dci-rhel-agent-ctl run) via `dci_jumpbox_execute`:
   ```
   dci_jumpbox_execute("ps aux | grep -E 'dci|ansible|podman'")
   dci_jumpbox_execute("podman ps -a")
   dci_jumpbox_execute("podman logs --tail 50 <container-name>")
   ```

   **Diagnostics suite** via `dci_ssh_diagnostics`:
   ```
   dci_ssh_diagnostics(context_hint="<phase: deployment, sap_prepare, benchmark, hana, storage, selinux, tuned>")
   ```

4. Search for related variables or references:

```bash
grep -rn "variable_or_role_name" dci-hooks/
```

5. Check `config-variables.yml` for default values that may conflict.

## Step 3: Plan

Write your plan in this exact format:

```
**PLAN:**
- **Root cause:** <what is actually wrong>
- **Evidence:** <specific error output or file content>
- **Proposed fix:** <what you will change, which file, which line>
- **Confidence:** High / Medium / Low — <why>
- **Risk:** <could this break something else?>
```

## Step 4: Apply the Fix

1. Edit the file(s):
   - Comment out broken lines with `# [AGENT-DISABLED]` prefix.
   - Add new/fixed lines with `# [AGENT-ADDED]` marker above.

2. Verify YAML syntax if editing `.yml` files:

```bash
python3 -c "import yaml; yaml.safe_load(open('<file>'))"
```

## Step 5: Commit and Push

1. Create a fix branch if not already on one:

```bash
git rev-parse --abbrev-ref HEAD | grep -q '^agent-fix/' || git checkout -b "agent-fix/$(date +%Y%m%d-%H%M%S)"
```

2. Commit:

```bash
git add <changed-files>
git commit -m "[agent-fix] <concise description of what and why>"
```

3. Push:

```bash
git push -u origin HEAD
```

4. If this is a new branch, create a PR:

```bash
gh pr list --head "$(git rev-parse --abbrev-ref HEAD)" --json number --jq length | grep -q '^0$' && \
  gh pr create --title "agent-fix: <short description>" --body "Single targeted fix applied by DCI agent."
```

## Step 6: Report

Print a summary:
- **Error:** what was wrong
- **Fix:** what was changed
- **File(s):** which files were modified
- **Commit:** the SHA
- **Next step:** "Run /dci-run to test this fix with a full workflow run."

Do NOT re-run the workflow. The user will do that separately or use `/dci-run`.
