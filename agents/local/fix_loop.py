"""
Fix loop state machine with handoff validation gates.

Enforces step completion during the /dci-run fix loop. The agent calls
submit_*() at each step; the gate validates that the step produced
complete outputs before the next step's actions become available.
PreToolUse hooks in .claude/hooks/ read fix_loop_state.json and block
actions belonging to steps the agent hasn't reached.

Design principle: LLM decides intent; code enforces invariants.
"""

import json
import logging
import subprocess
from pathlib import Path

from .filelock import atomic_write_json

logger = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).resolve().parent
_STATE_FILE = _STATE_DIR / "fix_loop_state.json"

VERBOSITY_SCHEDULE = [0, 2, 3, 4, 4]


def _empty_state(target_host="", topic="", error_output=""):
    return {
        "active": False,
        "target_host": target_host,
        "topic": topic,
        "attempt": 0,
        "max_attempts": 5,
        "triage_accepted": False,
        "triage_verified": True,  # Phase 3: set to False when builder/evaluator separation is implemented
        "plan_accepted": False,
        "fix_committed": False,
        "review_approved": False,
        "push_completed": False,
        "error_output": error_output[:2000],
        "triage_data": None,
        "plan_data": None,
        "fix_data": None,
        "fixes_history": [],
        "attempt_summaries": [],
        "subagent_used_this_attempt": False,
        "operator_hints": [],
    }


def _load():
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return _empty_state()
    return _empty_state()


def _save(state):
    atomic_write_json(_STATE_FILE, state)


def _result(accepted, **kwargs):
    return json.dumps({"accepted": accepted, **kwargs}, indent=2)


def _git_commit_exists(sha):
    if not sha:
        return False
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-1", sha],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


# --- Triage field requirements per action_type ---

_TRIAGE_REQUIRED = {
    "file_fix": ["file_path", "line", "correct_value"],
    "config_change": ["parameter", "target_value"],
    "infrastructure": ["component", "remediation_steps"],
    "escalate_to_human": ["description", "why_agent_cannot_fix"],
}

_TRIAGE_COMMON = ["failing_task", "phase", "evidence"]

_TRIAGE_HINTS = {
    "file_path": "Grep the codebase and jumpbox for the error value.",
    "line": "Read the file to find the exact line number.",
    "correct_value": "Read the file via cat on jumpbox — check comments for the correct value, or delegate to a subagent.",
    "evidence": "Include the grep output, file content, or SSH command result that proves your finding.",
    "parameter": "Identify the config parameter that needs changing.",
    "target_value": "Determine what the parameter should be set to.",
    "component": "Name the infrastructure component that failed (memory, disk, network, etc.).",
    "remediation_steps": "List the steps needed to fix the infrastructure issue.",
    "description": "Describe the issue in detail.",
    "why_agent_cannot_fix": "Explain why this requires human intervention.",
    "failing_task": "Extract the failing task name from the Ansible output.",
    "phase": "Identify the phase (1-5) from the task context.",
}


# --- Public API ---

def start_fix_loop(target_host, topic, error_output):
    state = _empty_state(target_host, topic, error_output)
    state["active"] = True
    state["attempt"] = 1
    _save(state)
    logger.info("Fix loop started for %s attempt 1", target_host)
    return _result(True, state="TRIAGE", attempt=1, max_attempts=5,
                   message="Fix loop started. Investigate the error and call submit_triage() with your findings.")


def get_fix_loop():
    state = _load()
    if not state.get("active"):
        return None
    return state


def end_fix_loop():
    state = _load()
    state["active"] = False
    _save(state)
    logger.info("Fix loop ended for %s", state.get("target_host", ""))


def mark_subagent_used():
    state = _load()
    state["subagent_used_this_attempt"] = True
    _save(state)


def submit_triage(action_type="", file_path="", line=0, wrong_value="",
                  correct_value="", evidence="", source="",
                  failing_task="", error_message="", phase=0,
                  parameter="", target_value="", component="",
                  remediation_steps=None, description="",
                  why_agent_cannot_fix="", suggested_human_actions=None,
                  operator_hint=""):
    state = _load()

    if not state.get("active"):
        return _result(False, error="No active fix loop. Call start_fix_loop() first.")

    if state.get("triage_accepted"):
        return _result(False, error="Triage already accepted. Proceed to submit_plan().")

    if operator_hint:
        state["operator_hints"].append(operator_hint)
        _save(state)

    if not action_type:
        return _result(False, error="Missing action_type. Choose: file_fix, config_change, infrastructure, escalate_to_human.",
                       hint="Investigate the error first, then decide what type of fix is needed.")

    if action_type not in _TRIAGE_REQUIRED:
        return _result(False, error=f"Unknown action_type '{action_type}'. Choose: file_fix, config_change, infrastructure, escalate_to_human.")

    # [AGENT-ADDED] Phase-based subagent enforcement: after the first attempt
    # for a phase-specific failure, require the expert subagent before accepting triage.
    _PHASE_REQUIRED_SUBAGENT = {
        1: ("os-deploy-expert", "Phase 1 (OS Deployment) failures MUST be investigated by the os-deploy-expert subagent."),
        3: ("hana-expert", "Phase 3 (HANA Installation) failures MUST be investigated by the hana-expert subagent."),
        4: ("hana-expert", "Phase 4 (PBO Benchmark) failures MUST be investigated by the hana-expert subagent."),
    }
    attempt = state.get("attempt", 1)
    if phase in _PHASE_REQUIRED_SUBAGENT and action_type != "escalate_to_human" and attempt >= 2:
        required_agent, msg = _PHASE_REQUIRED_SUBAGENT[phase]
        if source != required_agent and not state.get("subagent_used_this_attempt"):
            return _result(False,
                           error=f"{msg} Delegate to {required_agent}, call mark_subagent_used(), "
                                 f"then resubmit triage with source='{required_agent}'.",
                           hint=f"Use the {required_agent} subagent with the standard briefing template from DIAGNOSTICS.md.")

    if action_type == "escalate_to_human":
        if not state.get("subagent_used_this_attempt") and state.get("attempt", 1) <= 2:
            return _result(False,
                           error="Cannot escalate to human without first delegating to a subagent. "
                                 "Delegate to dci-diagnostician, hana-expert, or os-deploy-expert, "
                                 "then call mark_subagent_used(), then retry escalation if the subagent also can't solve it.",
                           hint="Choose a subagent based on the failure phase and error type.")

    locals_map = {
        "file_path": file_path, "line": line, "wrong_value": wrong_value,
        "correct_value": correct_value, "evidence": evidence, "source": source,
        "failing_task": failing_task, "error_message": error_message, "phase": phase,
        "parameter": parameter, "target_value": target_value, "component": component,
        "remediation_steps": remediation_steps, "description": description,
        "why_agent_cannot_fix": why_agent_cannot_fix,
        "suggested_human_actions": suggested_human_actions,
    }

    missing = []
    for field in _TRIAGE_COMMON:
        if not locals_map.get(field):
            missing.append(field)

    for field in _TRIAGE_REQUIRED[action_type]:
        val = locals_map.get(field)
        if val is None or val == "" or val == 0 or val == []:
            missing.append(field)

    if missing:
        hints = [_TRIAGE_HINTS.get(f, f"Provide {f}.") for f in missing]
        return _result(False,
                       error=f"Triage incomplete. Missing: {missing}.",
                       missing=missing,
                       hints=hints,
                       hint=" | ".join(hints))

    triage_data = {
        "action_type": action_type,
        "failing_task": failing_task,
        "error_message": error_message,
        "phase": phase,
        "evidence": evidence,
        "source": source,
    }
    for field in _TRIAGE_REQUIRED[action_type]:
        triage_data[field] = locals_map[field]

    state["triage_accepted"] = True
    state["triage_data"] = triage_data
    _save(state)

    logger.info("Triage accepted for %s: %s (%s)", state["target_host"], action_type, failing_task)

    return _result(True, state="PLAN", attempt=state["attempt"],
                   message="Triage accepted. Write your PLAN and call submit_plan().",
                   triage=triage_data)


def submit_plan(root_cause="", proposed_fix="", confidence="",
                fallback="", risk="", failure_category=""):
    state = _load()

    if not state.get("active"):
        return _result(False, error="No active fix loop.")

    if not state.get("triage_accepted"):
        return _result(False, error="Triage not accepted. Call submit_triage() first.")

    if state.get("plan_accepted"):
        return _result(False, error="Plan already accepted. Proceed to apply the fix.")

    missing = []
    if not root_cause:
        missing.append("root_cause")
    if not proposed_fix:
        missing.append("proposed_fix")
    if not confidence:
        missing.append("confidence")

    if missing:
        return _result(False, error=f"Plan incomplete. Missing: {missing}.", missing=missing)

    plan_data = {
        "root_cause": root_cause,
        "proposed_fix": proposed_fix,
        "confidence": confidence,
        "fallback": fallback,
        "risk": risk,
        "failure_category": failure_category,
    }

    state["plan_accepted"] = True
    state["plan_data"] = plan_data
    _save(state)

    triage = state.get("triage_data", {})
    file_hint = ""
    if triage.get("action_type") == "file_fix":
        file_hint = f" File to edit: {triage.get('file_path')} line {triage.get('line')}."

    logger.info("Plan accepted for %s: %s", state["target_host"], root_cause[:80])

    return _result(True, state="FIX", attempt=state["attempt"],
                   message=f"Plan accepted. Apply the fix, get ansible-reviewer approval, then call submit_fix().{file_hint}")


def submit_fix(commit_sha="", files_changed=None, description="",
               review_verdict="", fix_pattern=""):
    state = _load()

    if not state.get("active"):
        return _result(False, error="No active fix loop.")

    if not state.get("plan_accepted"):
        return _result(False, error="Plan not accepted. Call submit_plan() first.")

    if not commit_sha:
        return _result(False, error="No commit_sha. Commit your changes first.")

    if not _git_commit_exists(commit_sha):
        return _result(False, error=f"Commit {commit_sha} not found in git. Actually commit your changes, then provide the real SHA.")

    if not review_verdict:
        return _result(False, error="No review_verdict. Delegate to ansible-reviewer subagent first.",
                       hint="Use the ansible-reviewer subagent to review your change. Pass the verdict here.")

    if review_verdict.upper() == "REJECT":
        return _result(False, error="Review verdict is REJECT. Revise your fix based on the reviewer's feedback and re-submit.",
                       hint="Read the reviewer's reasons, adjust your fix, commit again, get a new review.")

    if review_verdict.upper() != "APPROVE":
        return _result(False, error=f"Invalid review_verdict '{review_verdict}'. Must be APPROVE or REJECT.")

    fix_data = {
        "commit_sha": commit_sha,
        "files_changed": files_changed or [],
        "description": description,
        "fix_pattern": fix_pattern,
    }

    state["fix_committed"] = True
    state["review_approved"] = True
    state["fix_data"] = fix_data
    _save(state)

    verbosity = VERBOSITY_SCHEDULE[min(state["attempt"] - 1, 4)]

    logger.info("Fix accepted for %s: %s", state["target_host"], commit_sha[:8])

    return _result(True, state="VERIFY", attempt=state["attempt"],
                   message=f"Fix accepted. Push and re-dispatch the workflow at verbosity={verbosity}.",
                   verbosity=verbosity)


def submit_result(success=False, phase_reached=0, failing_task="",
                  error_summary="", progress_assessment="",
                  assessment_evidence=""):
    state = _load()

    if not state.get("active"):
        return _result(False, error="No active fix loop.")

    if not state.get("fix_committed"):
        return _result(False, error="Fix not committed. Call submit_fix() first.")

    if success:
        state["fixes_history"].append({
            **(state.get("fix_data") or {}),
            "phase_reached": phase_reached,
            "outcome": "success",
            "kept": True,
        })
        state["active"] = False
        _save(state)

        fixes_kept = [f for f in state["fixes_history"] if f.get("kept")]
        logger.info("Fix loop SUCCESS for %s after %d attempts", state["target_host"], state["attempt"])

        return _result(True, done=True, success=True, attempts=state["attempt"],
                       fixes_kept=fixes_kept,
                       message="SUCCESS. Finalize: record to KB, generate change report, update PR, log end_run(success=True).")

    if not progress_assessment:
        return _result(False,
                       error="Missing progress_assessment. Compare this failure to the previous one. "
                             "Set to: progress (failure moved later), same (identical failure), "
                             "partial_progress (same task but further), regression (earlier failure), "
                             "or unfixable (cannot be resolved by the agent).",
                       hint="Include assessment_evidence explaining WHY you assessed this way.")

    if not assessment_evidence:
        return _result(False,
                       error=f"Missing assessment_evidence. Explain why you assessed '{progress_assessment}'.")

    keep = progress_assessment in ("progress", "partial_progress")

    state["fixes_history"].append({
        **(state.get("fix_data") or {}),
        "phase_reached": phase_reached,
        "outcome": progress_assessment,
        "kept": keep,
    })

    summary = {
        "attempt": state["attempt"],
        "error_investigated": state.get("error_output", "")[:200],
        "root_cause": (state.get("plan_data") or {}).get("root_cause", ""),
        "fix_applied": (state.get("fix_data") or {}).get("description", ""),
        "fix_outcome": progress_assessment,
        "fix_kept": keep,
        "key_learnings": assessment_evidence[:300],
        "subagents_used": [],
        "what_not_to_try_again": "" if keep else (state.get("plan_data") or {}).get("proposed_fix", ""),
    }
    state["attempt_summaries"].append(summary)

    if state["attempt"] >= state["max_attempts"]:
        state["active"] = False
        _save(state)

        fixes_to_revert = [f for f in state["fixes_history"] if not f.get("kept")]
        fixes_kept = [f for f in state["fixes_history"] if f.get("kept")]

        logger.info("Fix loop FAILURE for %s after %d attempts", state["target_host"], state["attempt"])

        return _result(True, done=True, success=False, attempts=state["attempt"],
                       fixes_to_revert=fixes_to_revert, fixes_kept=fixes_kept,
                       message="FAILURE — all attempts exhausted. Finalize: revert fixes that didn't advance progress, "
                               "create failure report PR, record to KB, log end_run(success=False).")

    revert_instruction = ""
    if not keep:
        sha = (state.get("fix_data") or {}).get("commit_sha", "")
        revert_instruction = f"REVERT first: git revert --no-edit {sha}. Then push the revert. "

    state["attempt"] += 1
    state["triage_accepted"] = False
    state["plan_accepted"] = False
    state["fix_committed"] = False
    state["review_approved"] = False
    state["push_completed"] = False
    state["triage_data"] = None
    state["plan_data"] = None
    state["fix_data"] = None
    state["error_output"] = error_summary[:2000]
    state["subagent_used_this_attempt"] = False
    _save(state)

    verbosity = VERBOSITY_SCHEDULE[min(state["attempt"] - 1, 4)]
    exploration = state["attempt"] >= 4

    logger.info("Fix loop attempt %d/%d for %s (%s)",
                state["attempt"], state["max_attempts"], state["target_host"], progress_assessment)

    msg = (f"{revert_instruction}"
           f"Attempt {state['attempt']}/{state['max_attempts']}. "
           f"Previous attempt: {progress_assessment}. "
           f"Investigate the new error and call submit_triage().")

    return _result(True, done=False, attempt=state["attempt"],
                   max_attempts=state["max_attempts"],
                   outcome=progress_assessment, kept=keep,
                   revert_instruction=revert_instruction,
                   verbosity_next=verbosity,
                   exploration_mode=exploration,
                   attempt_summaries=state["attempt_summaries"],
                   message=msg)


def check_stuck():
    state = _load()
    summaries = state.get("attempt_summaries", [])

    if len(summaries) < 2:
        return json.dumps({"stuck": False, "reason": "", "recommendation": ""})

    root_causes = [s.get("root_cause", "") for s in summaries]
    if len(set(root_causes[-3:])) == 1 and len(root_causes) >= 3:
        return json.dumps({
            "stuck": True,
            "reason": f"Same root cause repeated 3 times: {root_causes[-1][:100]}",
            "recommendation": "Enter exploration mode. Try a different subagent or investigate from a completely different angle.",
        })

    outcomes = [s.get("fix_outcome", "") for s in summaries]
    if all(o == "same" for o in outcomes[-2:]):
        return json.dumps({
            "stuck": True,
            "reason": "Last 2 fixes had no effect (outcome=same).",
            "recommendation": "The diagnosis may be wrong. Delegate to a different subagent for a fresh perspective.",
        })

    return json.dumps({"stuck": False, "reason": "", "recommendation": ""})
