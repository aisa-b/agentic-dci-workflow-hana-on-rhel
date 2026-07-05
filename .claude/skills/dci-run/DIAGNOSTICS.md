# Triage and Diagnosis Protocol

Every fix attempt starts with triage. Never guess. Never jump to fixing.

## 3a. Search the knowledge base (MANDATORY)

Before any investigation, search the knowledge base for matching past failures:

```python
from agents.skill_api import search_knowledge
results = search_knowledge("<error message from the failure>")
```

If matches are found:
- Check if a successful fix exists for this error pattern
- Include the matching diagnosis and fix in the subagent briefing
- If the exact fix was already applied and failed, skip it and try a different approach

If no matches: proceed to 3b with a fresh investigation.

## 3b. Read the failure output

From the workflow output you already have, extract:
- The **failing TASK name** (from the Ansible output)
- The **error message**
- The **phase** — identify based on the task file path, role name, or context:
  - `deploytype/`, `repos/`, `register`, `pre-run` → Phase 1 (OS Deployment)
  - `preconfigure`, `sap-general-preconfigure`, `sap-hana-preconfigure` → Phase 2 (OS Prep for HANA)
  - `sap_hana_install`, `hdblcm`, `hana_sid`, disk/volume setup → Phase 3 (HANA Installation)
  - `benchmark/`, `pbo/`, `pboscript` → Phase 4 (PBO Install and Run)
  - `reporting/`, `veris`, `junit` → Phase 5 (Results)

## 3b. Evaluate progress (attempts 2+)

Compare this failure to the previous one. This determines what happens to the
last fix — keep it, extend it, or revert it.

| Outcome | What it means | Action |
|---------|--------------|--------|
| Failure moved to a **later** task or phase | Fix worked. New issue exposed. | **Keep the fix.** Triage the NEW failure. |
| **Same** failure, exact same task | Fix didn't help. | **Revert the fix** (`git revert --no-edit <sha>`, push). Re-triage with higher verbosity. |
| Same failure but **more progress** within the task | Fix is partially right. | **Keep fix1.** Extend or modify it as fix2 (new commit on top). |
| Failure moved **earlier** | Fix broke something. | **Revert immediately** (`git revert --no-edit <sha>`, push). Re-triage. |

After reverting a failed fix, always increase verbosity on the next re-run to get
more detail about why the fix didn't help.

**Record the attempt outcome** (whether kept or reverted) so failed attempts
are preserved for future learning:

```bash
python3 -c "
from agents.skill_api import log_attempt_outcome
log_attempt_outcome('$RUN_ID', '<fqdn>', '<topic>', attempt_number=<N>,
    fix_sha='<sha>', fix_description='<what the fix did>',
    expected_outcome='<what you expected>', actual_outcome='<what happened>',
    what_was_learned='<key insight>', keep_or_revert='<kept|reverted>')
"
```

## 3c. Check KB category stats

Before investigating, check historical success rates for this type of failure:

```
get_category_stats()
```

Use the `agent_success_rate` for the matching failure category to calibrate
confidence in your PLAN block. For example, if `package_resolution` has a 73%
agent success rate, say "Confidence: High — package_resolution fixes succeed
73% historically." If a category has a high `human_intervention_rate`, flag
that the issue may require human input.

## 3d. Phase expectations check

Compare the actual server state from diagnostics against what the failed
phase (and its predecessor) should have left behind:

```bash
python3 -c "
from agents.skill_api import check_phase_expectations, format_phase_report
# Check the failed phase
report = format_phase_report(phase=<failed_phase>, actual_state={
    'tuned_profile': '<from diagnostics>',
    'selinux_mode': '<from diagnostics>',
    'ssh_accessible': True,
    'os_installed': True,
    'hana_installed': <True/False>,
    'hana_running': <True/False>,
    'sidadm_user_exists': <True/False>,
}, elapsed_minutes=<elapsed>, target_host='<fqdn>')
print(report)
# Check the PREVIOUS phase — if its expectations aren't met, root cause is upstream
if <failed_phase> > 1:
    print()
    print('Previous phase check:')
    print(format_phase_report(phase=<failed_phase>-1, actual_state={...}, target_host='<fqdn>'))
"
```

Use deviations to inform your diagnosis. If the previous phase's expected
state is not met, focus investigation there — the root cause is upstream.

## 3e. Local codebase investigation

Before any remote investigation, check the local Ansible codebase:

1. **Read the failing task file** in `dci-hooks/`.
2. **Grep for the failing variable, role, or package** across the hooks directory:

```bash
grep -rn "variable_or_role_name" dci-hooks/
```

3. **Read `config-variables.yml`** for relevant defaults.
4. **Check if this is a known pattern** from the knowledge base.

## 3f. Decide: local finding or remote investigation?

**If local analysis explains the root cause** (missing package in list, wrong `when:`
condition, role version mismatch, variable typo, etc.):
→ Proceed to Step 4 with your local findings as the diagnosis.

**If local analysis is inconclusive** (server state issue, runtime error,
filesystem/permission problem, package actually missing from repos, etc.):
→ You MUST delegate to a subagent. Do NOT perform your own SSH investigation.

## 3g. Delegate to the right subagent

When local analysis is insufficient, delegate diagnosis to one of these subagents.
Choose based on what the failure output tells you — these are guidelines, not fixed rules.

**`dci-diagnostician`** — best for:
- OS-level issues (kernel, SELinux, tuned profile, systemd)
- Package/repo problems (missing packages, disabled repos, subscription issues)
- Storage, network, and filesystem problems
- Ansible role failures during OS deployment or SAP prep phases
- General "something is wrong with the server" situations

**`hana-expert`** — best for:
- HANA installation failures (`hdbinst.log`, `hdblcm.log`)
- HANA process issues (nameserver, indexserver, sapstartsrv)
- sidadm user or sapcontrol problems
- PBOffline benchmark failures
- `/hana/` filesystem or mount issues
- SAP Host Agent problems

**`os-deploy-expert`** — best for:
- Phase 1 (OS Deployment) failures exclusively
- Kickstart / partitioning problems ("kickstart insufficient", "installation destination")
- PXE boot failures (server never starts install, DHCP/TFTP issues)
- Install timeouts (Anaconda stuck at interactive prompt)
- Post-install SSH access failures (wrong password, connection refused)
- BIOS boot order issues (disk before network)
- BMC/iLO state investigation (power status, system event log, boot device)
- RHEL version-specific partition requirements (UEFI vs legacy BIOS)

**Both** — when the failure spans domains (e.g. a benchmark failure that might
be caused by an OS-level storage issue, or a HANA install failure that might be
caused by missing OS packages).

Use the standardized briefing template for EVERY subagent invocation. Do not
improvise the briefing -- consistency ensures the subagent has all the context
it needs and produces comparable reports across invocations.

**Briefing template for dci-diagnostician:**
```
Investigate a DCI workflow failure on <fqdn>.
Target: <fqdn>
Failing task: <exact task name from Ansible output>
Error: <exact error message>
Phase: <1-5> (<phase name>)
RHEL topic: <RHEL-X.Y>
Local analysis findings: <what was checked locally and what was found>
Prior fix attempts: <list of fixes tried and their outcomes, or "first attempt">
Focus area: <specific hint: selinux, tuned, storage, packages, network>
```

**Briefing template for hana-expert:**
```
Investigate a SAP HANA failure on <fqdn>.
Target: <fqdn>
HANA SID: HDB (Instance 10, user hdbadm)
Failing task: <exact task name>
Error: <exact error message>
Phase: <3 or 4> (<HANA Install or PBO Benchmark>)
RHEL topic: <RHEL-X.Y>
Local analysis findings: <what was checked locally>
Prior fix attempts: <list or "first attempt">
Focus area: <installation / process health / benchmark / trace files>
```

**Briefing template for os-deploy-expert:**
```
Investigate a Phase 1 (OS Deployment) failure on <fqdn>.
Target: <fqdn>
Power address (BMC): <hostname>r.example.corp
Failing task: <exact task name>
Error: <exact error message>
Failure sub-phase: <PXE / kickstart / partitioning / post-install / SSH>
RHEL topic: <RHEL-X.Y>
Settings file: settings/settings_current_<hostname>.yml
Disk map entry: <from run_config.yml>
Local analysis findings: <what was checked locally>
Prior fix attempts: <list or "first attempt">
```

**Briefing template for ansible-reviewer:**
```
Review the following Ansible change before commit.
Files changed: <list of file paths>
Original failing task: <task name and what it did>
What was wrong: <why it failed>
What changed: <what was commented out with [AGENT-DISABLED], what was added with [AGENT-ADDED]>
Phase: <1-5> (<phase name>)
Context: <why this fix should resolve the failure>
```

The subagent will write a full report to `agents/local/diagnosis_reports/`
and return a short summary with: root cause, confidence, recommended fix,
and file path.

## 3g-err. Subagent failure handling

If the subagent returns without a structured summary (missing root cause,
confidence, or recommended fix), or crashes, or hits its turn limit:

1. **Retry once:** re-invoke the same subagent with: "Your previous response
   was incomplete. Return exactly: Root cause (one sentence), Confidence
   (High/Medium/Low), Recommended fix (one sentence), Full report path."
2. **If second attempt also fails:** pivot to a different subagent for the
   same failure. If dci-diagnostician failed, try hana-expert (or vice versa
   depending on the phase). The failure might be in a domain the first
   subagent doesn't cover well.
3. **If no subagent produces a usable diagnosis:** escalate to the user.
   Report what was tried, what each subagent returned, and ask for guidance.

Do NOT proceed to Step 4 (Plan) without a diagnosis. No guessing.

## 3h. Check run journal for cross-run patterns

Search the journal for similar past diagnoses (including failed attempts that
the KB doesn't store):

```bash
python3 -c "
import json
from agents.skill_api import search_diagnoses, find_pattern
results = search_diagnoses('<error message>', threshold=0.4)
if results: print('Similar past diagnoses:', json.dumps(results[:3], indent=2, default=str))
pattern = find_pattern(failing_task='<task>', phase=<N>, rhel_topic='<topic>')
if pattern['match_count']: print('Recurring pattern:', json.dumps(pattern, indent=2, default=str))
"
```

## 3i. Journal: Log triage and diagnosis

After completing all triage steps, record your findings:

```bash
python3 -c "
from agents.skill_api import log_triage, log_diagnosis
log_triage('$RUN_ID', '<fqdn>', '<topic>', attempt_number=<N>, failing_task='<task name>', error_message='<error text>', phase=<1-5>, prior_attempt_outcome='<first_run|progress|same|regression>')
log_diagnosis('$RUN_ID', '<fqdn>', '<topic>', attempt_number=<N>, source='<local_analysis|dci-diagnostician|hana-expert|os-deploy-expert|combined>', findings='<your full diagnostic findings>', commands_run=[<list of commands>], kb_matches_found=<N>, cause_event_id='<triage event_id if available>')
"
```

Save the returned `event_id` from `log_diagnosis()` — pass it as `cause_event_id`
to `log_plan()` so the causal chain links diagnosis → plan → fix.

## 3j. Submit triage to the gate (MANDATORY)

After all triage steps are complete, submit your findings to the fix loop gate.
The gate validates that all required fields are present for the action type.

```bash
python3 -c "
from agents.skill_api import submit_triage
print(submit_triage(
    action_type='file_fix',  # or: config_change, infrastructure, escalate_to_human
    file_path='<path>',
    line=<N>,
    wrong_value='<current wrong value>',
    correct_value='<what it should be>',
    evidence='<grep output, file content, or SSH result proving this>',
    source='<local_analysis|dci-diagnostician|hana-expert|os-deploy-expert>',
    failing_task='<exact task name>',
    phase=<1-5>
))
"
```

**If rejected:** The gate returns hints telling you what's missing and how to
find it. Follow the hints and re-submit. Common rejections:
- `missing correct_value` — read the file via `cat` on the jumpbox, check comments
- `missing evidence` — include the grep or SSH output that proves your finding
- `escalate_to_human without subagent` — delegate to a subagent first

**Action types:**
- `file_fix` — you know the file, line, and correct value (most common)
- `config_change` — a config parameter needs changing (requires `parameter`, `target_value`)
- `infrastructure` — hardware/resource issue (requires `component`, `remediation_steps`)
- `escalate_to_human` — cannot be fixed by the agent (requires `description`, `why_agent_cannot_fix`)

Do NOT proceed to Step 4 until `submit_triage()` returns `accepted: true`.
