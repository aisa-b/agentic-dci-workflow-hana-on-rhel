# Fix, Review, Verify, and Evaluate

## 5a. Apply the fix

Every fix follows this pattern:

1. **Comment out the failing Ansible task or block** with `#` prefix and `# [AGENT-DISABLED]` marker.
   The original code is ALWAYS preserved — never delete it.
2. **Add the new/fixed Ansible task(s)** below the commented-out code, with `# [AGENT-ADDED]`
   marker on the line above each new block.

This means both the old and new code coexist in the file. The old code serves as
documentation of what was there before. The new code is what runs.

If this is fix2 extending a partially-right fix1, add the new code alongside fix1
(do not comment out fix1 since it was working). Mark the extension with
`# [AGENT-ADDED fix2]`.

## 5b. Review gate (MANDATORY)

Delegate to the `ansible-reviewer` subagent to validate your change. The reviewer
checks:
- The failing task is commented out (not deleted) with `# [AGENT-DISABLED]` marker
- The new task(s) have `# [AGENT-ADDED]` markers
- YAML syntax is valid
- Variable references are correct
- No cross-phase breakage
- No security issues

Brief it with:
- Which file(s) you changed
- What the original failing task was
- What the new task(s) do differently
- The context (which phase, what error it fixes)

**If APPROVE:** proceed to 5c.

**If REJECT:** the reviewer will explain what's wrong. Revise your fix and
re-submit for review. Maximum 2 review rounds — if still rejected after 2 rounds,
this attempt counts as failed. Go back to Step 3 for the next attempt with a
different approach.

## 5c. Commit

```bash
git add <changed-files>
git commit -m "[agent-fix attempt N] <concise description of what and why>"
```

## 5d. Push

```bash
git push -u origin HEAD
```

If the fix modified files in the hooks repo, sync them:

```bash
python3 -c "
from agents.skill_api import sync_hooks
import json
result = sync_hooks(commit_message='[agent-fix attempt N] <description>')
print(json.dumps(result, indent=2))
"
```

The relay pulls the hooks repo on the jumpbox before each workflow re-run
automatically — no `dci_relay_update()` needed for hooks-only changes.

## 5e. Create PR (first fix only)

```bash
gh pr create --title "agent-fix: <short description>" --body "Automated fix by DCI agent. Will be updated with subsequent attempts."
```

Subsequent pushes update the same PR automatically.

## 5f. Submit fix to the gate (MANDATORY)

After committing and getting reviewer approval, submit the fix:

```bash
python3 -c "
from agents.skill_api import submit_fix
print(submit_fix(
    commit_sha='<sha>',
    files_changed=['<file1>', '<file2>'],
    description='<what the fix does>',
    review_verdict='APPROVE'
))
"
```

The gate verifies: commit SHA exists in git, review verdict is APPROVE.
If rejected, follow the feedback (commit first, or get review first).
Do NOT push or dispatch until `submit_fix()` returns `accepted: true`.

## Journal: Log fix applied

The gate logs the fix automatically. For manual logging:

```bash
python3 -c "
from agents.skill_api import log_fix_applied
log_fix_applied('$RUN_ID', '<fqdn>', '<topic>', attempt_number=<N>, files_changed=[<files>], commit_sha='<sha>', commit_message='<message>', review_result='<approved|rejected_then_revised>', review_rounds=<1|2>, cause_event_id='<plan event_id>')
"
```

When reverting a fix (Step 3b), also log:

```bash
python3 -c "
from agents.skill_api import log_fix_reverted
log_fix_reverted('$RUN_ID', '<fqdn>', '<topic>', attempt_number=<N>, revert_reason='<same_failure|regression|final_cleanup>', original_commit_sha='<sha>', revert_commit_sha='<revert sha>')
"
```

## 6a. Runtime verification (BEFORE re-running the full workflow)

After pushing the fix but BEFORE triggering a 2-hour workflow re-run,
verify the fix directly via SSH when possible. This saves 2 hours on
bad fixes.

| Fix type | Verification command |
|----------|---------------------|
| Missing package | `dci_ssh_execute("rpm -q <package-name>")` or `dci_ssh_execute("dnf install --assumeno <package>")` |
| SELinux context | `dci_ssh_execute("semanage fcontext -l \| grep <path>")` |
| tuned profile | `dci_ssh_execute("tuned-adm verify")` |
| File permissions | `dci_ssh_execute("ls -la <path>")` |
| Service startup | `dci_ssh_execute("systemctl status <service>")` |

If the verification command shows the fix didn't take effect (package
still missing, context still wrong), do NOT re-run. Fix the fix first.

If the fix is in Ansible hooks code (not server state), verification
isn't possible -- proceed directly to 6b.

## 6b. Re-run with increasing verbosity

Run the full DCI workflow from scratch with escalating verbosity:

| Attempt | Verbosity | Purpose |
|---------|-----------|---------|
| 1 | 0 | Baseline |
| 2 | 2 | Task-level detail |
| 3 | 3 | Connection-level detail |
| 4+ | 4 | Full debug |

```
dci_workflow_run(
    verbosity=<level>,
    settings_file="/etc/dci-rhel-agent/settings_current_<hostname>.yml",
    target_host="<fqdn>"
)
```

Always pass the same `settings_file` and `target_host` from Step 0.
Every re-run deploys from scratch — the target gets a fresh OS.

## 6c. Evaluate the result and submit to the gate

Compare this failure to the previous one, then submit your assessment:

```bash
python3 -c "
from agents.skill_api import submit_result
print(submit_result(
    success=<True|False>,
    phase_reached=<1-5>,
    failing_task='<task name>',
    error_summary='<first 500 chars of error>',
    progress_assessment='<progress|same|partial_progress|regression|unfixable>',
    assessment_evidence='<explain WHY you assessed this way>'
))
"
```

The gate returns what to do next:
- `done: true, success: true` → Go to Step 7 (success path)
- `done: false` → Follow the returned instructions (revert if needed, triage new error)
- `done: true, success: false` → All attempts exhausted, go to Step 7 (failure path)

The gate also provides `attempt_summaries` from prior attempts — read these
to avoid repeating the same approach.

**Before the next attempt, check if the loop is stuck:**

```bash
python3 -c "
from agents.skill_api import check_stuck
print(check_stuck())
"
```

If stuck, follow the recommendation (different subagent, exploration mode).

**Progress assessment guide:**
- **progress** — failure moved to a later task or phase. Keep the fix.
- **same** — identical failure, same task. Revert the fix.
- **partial_progress** — same task but further within it. Keep the fix, extend it.
- **regression** — failure moved earlier. Revert immediately.
- **unfixable** — cannot be resolved by the agent. Write failure report.

## Exploration Mode (after 3 failed attempts)

If 3 fix attempts have been applied and none fully resolved the issue, **STOP trying
to fix**. Enter exploration mode:

1. **Do NOT edit any files.**
2. Run the workflow with `verbosity=4` (maximum detail).
3. Delegate to the relevant subagents for comprehensive investigation:
   - `dci-diagnostician` — full server state assessment
   - `hana-expert` — full HANA health report
   - `os-deploy-expert` — if the failure is in Phase 1 (OS deployment, kickstart, PXE, partitioning)
4. Combine their findings.
5. Decide:
   - **Fixable with high confidence?** Apply attempt 4 based on combined findings.
   - **Deeper issue (kernel, hardware, network, upstream bug)?** Go directly to
     failure report (Step 7, failure path).
