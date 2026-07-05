---
name: dci-report
description: Generate a detailed failure report PR
disable-model-invocation: true
---

# DCI Failure Report

Generate a comprehensive failure report, revert all agent changes, and create a PR
documenting the investigation. Use this after a failed `/dci-run` or when manually
concluding that a failure is beyond automated fixing.

## Input

`$ARGUMENTS` may contain additional context about the failure (optional).

## Safety Rules

- **NEVER delete code.** Reverts are done via `git revert` (new commits, not history rewriting).
- **Git push ONLY to** `origin` (`github.com/aisa-b/agentic-dci-workflow`).
- **NEVER add `Co-authored-by` to commit messages.**

## Step 1: Gather Information

1. Read `run_config.yml` for target, RHEL topic, and settings.

2. Check the current branch and commit history:

```bash
git log --oneline main..HEAD
```

3. Collect diagnostic data from the target via MCP tools:

```
dci_ssh_diagnostics(context_hint="deployment")
dci_ssh_diagnostics(context_hint="sap_prepare")
dci_ssh_diagnostics(context_hint="benchmark")
```

4. Run targeted SSH checks:

```
dci_ssh_execute("cat /etc/redhat-release")
dci_ssh_execute("getenforce")
dci_ssh_execute("tuned-adm active")
dci_ssh_execute("systemctl list-units --failed")
dci_ssh_execute("journalctl --no-pager -p err -n 50")
dci_ssh_execute("dmesg | tail -30")
```

5. Read the knowledge base for any related past entries:

```bash
cat agents/local/knowledge_base.json 2>/dev/null
```

6. If `$ARGUMENTS` contains additional failure context, incorporate it.

## Step 2: Write the Failure Report

Compose a detailed report following this template exactly:

```markdown
## Failure Report

**Target:** <hostname from run_config.yml>
**RHEL Topic:** <rhel_topic from run_config.yml>
**Date:** <current timestamp>
**Result:** FAILED after <N> attempts — all changes reverted

---

### Original Failure
- **Failed task:** <Ansible task name from error output>
- **Error message:** <exact error message>
- **Phase:** <OS Deployment | SAP Environment Prep | PBOffline Benchmark | Results Collection>

---

### Attempt 1
- **Diagnosis:** <what was found during investigation>
- **Fix applied:** <what was changed, which file, which line>
- **Commit:** <SHA from git log>
- **Result:** <still failed / new error / progressed to later phase>

### Attempt 2
... (repeat for each attempt found in git log)

---

### Server State at Time of Report
- **OS:** <output of cat /etc/redhat-release>
- **SELinux:** <output of getenforce>
- **Tuned profile:** <output of tuned-adm active>
- **Failed services:** <output of systemctl list-units --failed>

---

### Root Cause Analysis
<your best understanding of why the failure could not be fixed automatically>

### Recommendations for Human Operator
- <specific actionable items>
- <files to check manually>
- <commands to run>
- <upstream bugs or version incompatibilities identified>
- <possible causes requiring human judgment>
```

## Step 3: Revert All Agent Changes

If there are agent fix commits on the current branch (commits after diverging from main):

```bash
git log --oneline --reverse main..HEAD | grep '\[agent-fix'
```

Revert each fix commit in **reverse chronological order** (newest first):

```bash
git revert --no-edit <sha>
```

If a revert conflicts, abort and note it in the report:

```bash
git revert --abort
```

Push the reverts:

```bash
git push origin HEAD
```

## Step 4: Create the Failure Report PR

```bash
gh pr create \
  --title "agent-fix: FAILED — <one-line failure summary>" \
  --body "<full failure report from Step 2>"
```

If a PR already exists on this branch, update it instead:

```bash
gh pr edit --body "<full failure report from Step 2>"
```

## Step 5: Record to Knowledge Base

For each attempt, add an entry to the knowledge base with `success: false`:

```bash
python3 -c "
import json, datetime
from pathlib import Path

kb_path = Path('agents/local/knowledge_base.json')
entries = json.loads(kb_path.read_text()) if kb_path.exists() else []
entries.append({
    'timestamp': datetime.datetime.now().isoformat(),
    'error_pattern': '<error pattern>',
    'diagnosis': '<diagnosis>',
    'fix_applied': '<fix applied>',
    'files_changed': ['<files>'],
    'success': False,
    'target_host': '<target>',
    'rhel_version': '<rhel_topic>',
})
kb_path.parent.mkdir(parents=True, exist_ok=True)
kb_path.write_text(json.dumps(entries, indent=2))
"
```

## Step 6: Final Summary

Print:
- Number of attempts made
- All commits (fix + revert)
- PR URL
- Top recommendation for human operator
