# Active Monitoring During Workflow Runs

## Fleet-aware monitoring

Use `dci_fleet_status()` instead of individual `dci_workflow_status()` calls.
One call returns all running workflows with phase info, heartbeat state, and
recent completions. Print the dashboard in this format:

```
[HH:MM] Fleet (N active, M complete, K failed)
  target-1: [2/3] Phase 3 (HANA Install) 42m
  target-2: [1/2] Phase 2 (SAP Prep) 28m
  target-5: Phase 1 (OS Deploy) 15m
```

Every poll shows every workflow. Hostname first. Full status every time.

Poll every 2-3 minutes. On each poll:
- Print the dashboard
- If a workflow shows `alert`, investigate that server
- If a workflow completed successfully, check fleet state for `nr` re-dispatch
- If a workflow failed, enter triage for that target (Steps 2-6)

## Fleet state tracking

On dispatch, register the goal in fleet state:

```python
from agents.skill_api import set_goal
set_goal("<fqdn>", nr=<N>, topic="<topic>")
```

On completion, update the counter:

```python
from agents.skill_api import record_completion, should_redispatch
record_completion("<fqdn>", success=True, elapsed=<seconds>)
if should_redispatch("<fqdn>"):
    # dispatch next run as independent job
    dci_workflow_run(...)
```

## Per-server investigation (when alerts trigger)

1. **Syslog on jumpbox** — Anaconda streams logs to the jumpbox:
   ```
   dci_jumpbox_execute("sudo cat /var/log/messages | grep -i anaconda | tail -30")
   dci_jumpbox_execute("sudo cat /var/log/messages | grep <hostname> | tail -20")
   ```
   Use `sudo` — the jumpbox user cannot read `/var/log/messages` directly.

2. **IPMI hardware state** — check server power and events:
   ```
   dci_jumpbox_execute("sudo ipmitool -I lanplus -H <power_address> -U <user> -P <pass> power status")
   dci_jumpbox_execute("sudo ipmitool -I lanplus -H <power_address> -U <user> -P <pass> sel elist last 10")
   ```

3. **Network reachability** — verify target is alive during install:
   ```
   dci_jumpbox_execute("ping -c 2 -W 3 <fqdn>")
   ```

4. **Phase timing check** — on every poll, check if the current phase is
   overdue using learned per-server baselines:
   ```bash
   python3 -c "
   from agents.skill_api import detect_phase_number, is_phase_overdue, get_phase_timing
   phase_num = detect_phase_number('<phase string from heartbeat>')
   if phase_num:
       timing = get_phase_timing(phase_num, target_host='<fqdn>')
       overdue = is_phase_overdue(phase_num, <elapsed_minutes>, target_host='<fqdn>')
       source = timing.get('source', 'static')
       print(f'Phase {phase_num} ({timing[\"name\"]}): {<elapsed_minutes>:.0f}min elapsed, '
             f'max {timing[\"max_minutes\"]}min [{source}] — {\"OVERDUE\" if overdue else \"on track\"}')
   "
   ```
   If OVERDUE: dispatch the relevant expert subagent (os-deploy-expert for
   Phase 1, hana-expert for Phase 3). Do NOT wait for a failure — the clock
   running out IS the signal to investigate.

## Stuck job detection

If a phase is OVERDUE per the timing check above, flag as STUCK in the
dashboard. Dispatch the relevant expert subagent to investigate. If
the process is dead but the relay hasn't noticed, call `dci_workflow_stop()`
to clean relay state, then re-dispatch.

## Missed completion verification (MANDATORY)

If a workflow's result was missed (monitoring gap, laptop sleep, session
crash), NEVER assume success based on indirect evidence (HANA running,
server reachable, etc.). The actual Ansible return code and play recap
are the only proof of success.

To verify a missed completion, try every source in order:
1. **Relay completions:** check `dci_fleet_status()` or `dci_workflow_list()`
   -- the result is stored for 60 minutes with success/failure and error summary
2. **Pub/Sub:** poll `dci_workflow_status(target_host)` with the original
   correlation ID -- the result message may still be in the subscription
3. **DCI backend:** check the jumpbox for the job result:
   ```
   dci_jumpbox_execute("sudo dci-rhel-agent-ctl --config <settings> --status 2>/dev/null | tail -5")
   ```
4. **Server state:** SSH to the target and check HANA health, PBO results,
   /etc/redhat-release -- indirect evidence, not proof, but better than nothing
5. **If all sources are exhausted:** mark as `completed-unknown` in fleet state
   and re-dispatch the run. A wasted re-run is better than a false success count.

Exhaust all available information before giving up. Only mark as unknown
when every source has been tried and none has the result.

## Zombie process detection

On startup, check the jumpbox for `dci-rhel-agent-ctl` processes not tracked
by the relay. Flag as ZOMBIE in the dashboard. Let the user decide whether
to kill or let finish.

## Timeout and escalation rules (MANDATORY)

These are hard time bounds. Never exceed them silently.

- **MCP tool call >30s with no response:** Report "no response after 30s"
  and try an alternative (different tool, different approach).
- **MCP tool call >60s:** Stop waiting. Report the failure to the user
  with what you know. Try a completely different diagnostic path.
- **Two consecutive polls with no progress:** Something is wrong.
  Check the jumpbox process directly, check the relay, report findings.
- **Heartbeat age >120s:** Don't wait for the next poll. Check jumpbox
  immediately with `ps aux | grep dci-rhel-agent-ctl`.
- **Never wait silently.** If you find yourself doing nothing for more
  than 10 seconds, something is wrong. Diagnose and report.

## SRE troubleshooting methodology (MANDATORY)

Follow the Google SRE approach for all debugging:

1. **Collect evidence first.** Read every log line, error, warning, and
   response before forming any theory. List each one with a one-line
   explanation.
2. **Present evidence to the user.** Show what you see before what you
   think. "Here are the facts: X, Y, Z. Based on this, I think..."
3. **Evidence trumps theory.** If any piece of evidence contradicts your
   diagnosis, stop and reassess. A BLOCKED command means the relay is
   alive -- never conclude "relay down" when you have proof it responded.
4. **5 Whys.** Don't stop at the first answer. "Timeout" -> why? ->
   "No response" -> why? -> "Command blocked" -> why? -> "Not on
   allowlist" -> fix the command, not the relay.
5. **Mitigate first, root-cause second.** Make it work, then figure out
   why it broke.
