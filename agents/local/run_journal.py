"""
Run Journal -- event-sourced telemetry for DCI workflow runs.

Captures the full story of each /dci-run invocation: what was tried,
what reasoning led to each decision, what failed, and what succeeded.
Append-only JSONL format for crash safety and greppability.

Layers on top of knowledge_base.py -- the journal records the JOURNEY,
the KB records the FIXES. They cross-reference by run_id.
"""

import datetime
import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_DIR = Path("/tmp/dci-agent-logs")
try:
    from .. import config
    _LOG_DIR = config.LOG_DIR
except Exception:
    pass

_JOURNAL_PATH = _LOG_DIR / "run_journal.jsonl"


def _emit(
    event_type: str,
    run_id: str,
    target_host: str,
    rhel_topic: str,
    data: dict,
    cause_event_id: str = "",
) -> dict:
    event = {
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "event_type": event_type,
        "target_host": target_host,
        "rhel_topic": rhel_topic,
        "data": data,
    }
    from .filelock import locked_append
    locked_append(_JOURNAL_PATH, json.dumps(event, default=str))
    logger.debug("Journal event: %s for run %s", event_type, run_id[:8])

    try:
        from .events import emit as unified_emit
        unified_emit(
            f"journal.{event_type}",
            run_id=run_id,
            target_host=target_host,
            rhel_topic=rhel_topic,
            attempt_number=data.get("attempt_number", 0),
            phase=data.get("phase", data.get("phase_reached", 0)),
            data=data,
            cause_event_id=cause_event_id,
        )
    except Exception as e:
        logger.debug("Unified event forward failed: %s", e)

    return event


def _try_embed(text: str) -> list[float]:
    try:
        from .knowledge_base import _embed
        return _embed(text[:2000])
    except Exception:
        return []


# ── Lifecycle ─────────────────────────────────────────────────────────

def start_run(
    target_host: str,
    rhel_topic: str,
    server_profile: dict | None = None,
    kb_entries_at_start: int = 0,
    human_fixes_ingested: int = 0,
) -> str:
    run_id = str(uuid.uuid4())
    _emit("run_started", run_id, target_host, rhel_topic, {
        "hostname": target_host.split(".")[0],
        "server_profile": server_profile,
        "kb_entries_at_start": kb_entries_at_start,
        "human_fixes_ingested": human_fixes_ingested,
    })
    return run_id


def end_run(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    success: bool,
    total_attempts: int,
    fixes_kept: list[str] | None = None,
    fixes_reverted: list[str] | None = None,
    final_phase_reached: int = 0,
    pr_url: str = "",
    failure_category: str = "",
    kb_entry_ids: list[str] | None = None,
    server_profile_after: dict | None = None,
) -> dict:
    started = next(
        (e for e in _load_all() if e["run_id"] == run_id and e["event_type"] == "run_started"),
        None,
    )
    start_ts = started["timestamp"] if started else None
    elapsed = 0
    if start_ts:
        try:
            dt = datetime.datetime.fromisoformat(start_ts)
            elapsed = int((datetime.datetime.now() - dt).total_seconds())
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse run start timestamp '%s': %s", start_ts, e)

    return _emit("run_completed", run_id, target_host, rhel_topic, {
        "success": success,
        "total_attempts": total_attempts,
        "total_elapsed_seconds": elapsed,
        "fixes_kept": fixes_kept or [],
        "fixes_reverted": fixes_reverted or [],
        "final_phase_reached": final_phase_reached,
        "pr_url": pr_url or None,
        "failure_category": failure_category or None,
        "kb_entry_ids": kb_entry_ids or [],
        "server_profile_after": server_profile_after,
    })


# ── Workflow events ───────────────────────────────────────────────────

def log_workflow_dispatched(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    verbosity: int = 0,
    settings_file: str = "",
    correlation_id: str = "",
) -> dict:
    return _emit("workflow_dispatched", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "verbosity": verbosity,
        "settings_file": settings_file,
        "correlation_id": correlation_id,
    })


def log_workflow_completed(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    success: bool,
    elapsed_seconds: int = 0,
    phase_reached: int = 0,
    failing_task: str = "",
    error_summary: str = "",
    output_lines: int = 0,
) -> dict:
    return _emit("workflow_completed", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "success": success,
        "elapsed_seconds": elapsed_seconds,
        "phase_reached": phase_reached,
        "failing_task": failing_task,
        "error_summary": error_summary[:500],
        "output_lines": output_lines,
    })


# ── Diagnosis events ─────────────────────────────────────────────────

def log_triage(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    failing_task: str,
    error_message: str,
    phase: int,
    prior_attempt_outcome: str = "first_run",
    category_stats_snapshot: dict | None = None,
) -> dict:
    return _emit("triage_started", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "failing_task": failing_task,
        "error_message": error_message[:500],
        "phase": phase,
        "prior_attempt_outcome": prior_attempt_outcome,
        "category_stats_snapshot": category_stats_snapshot or {},
    })


def log_diagnosis(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    source: str,
    findings: str,
    commands_run: list[str] | None = None,
    logs_read: list[str] | None = None,
    kb_matches_found: int = 0,
    kb_match_used: str = "",
    cause_event_id: str = "",
) -> dict:
    data = {
        "attempt_number": attempt_number,
        "source": source,
        "findings": findings,
        "commands_run": commands_run or [],
        "logs_read": logs_read or [],
        "kb_matches_found": kb_matches_found,
        "kb_match_used": kb_match_used or None,
        "_embedding": _try_embed(findings),
    }
    return _emit("diagnosis_recorded", run_id, target_host, rhel_topic, data, cause_event_id=cause_event_id)


def log_plan(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    root_cause: str,
    evidence: str,
    proposed_fix: str,
    confidence: str,
    confidence_rationale: str = "",
    fallback: str = "",
    risk: str = "",
    failure_category: str = "",
    cause_event_id: str = "",
) -> dict:
    plan_text = f"{root_cause} {proposed_fix} {evidence}"
    data = {
        "attempt_number": attempt_number,
        "root_cause": root_cause,
        "evidence": evidence,
        "proposed_fix": proposed_fix,
        "confidence": confidence,
        "confidence_rationale": confidence_rationale,
        "fallback": fallback,
        "risk": risk,
        "failure_category": failure_category,
        "_embedding": _try_embed(plan_text),
    }
    return _emit("plan_recorded", run_id, target_host, rhel_topic, data, cause_event_id=cause_event_id)


# ── Fix events ────────────────────────────────────────────────────────

def log_fix_applied(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    files_changed: list[str],
    commit_sha: str,
    commit_message: str = "",
    review_result: str = "approved",
    review_rounds: int = 1,
    cause_event_id: str = "",
) -> dict:
    return _emit("fix_applied", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "files_changed": files_changed,
        "commit_sha": commit_sha,
        "commit_message": commit_message,
        "review_result": review_result,
        "review_rounds": review_rounds,
    }, cause_event_id=cause_event_id)


def log_fix_reverted(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    revert_reason: str,
    original_commit_sha: str,
    revert_commit_sha: str = "",
) -> dict:
    return _emit("fix_reverted", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "revert_reason": revert_reason,
        "original_commit_sha": original_commit_sha,
        "revert_commit_sha": revert_commit_sha,
    })


# ── Attempt outcome tracking ─────────────────────────────────────────

def log_attempt_outcome(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    attempt_number: int,
    fix_sha: str,
    fix_description: str,
    expected_outcome: str,
    actual_outcome: str,
    what_was_learned: str,
    keep_or_revert: str,
    cause_event_id: str = "",
) -> dict:
    """Record the outcome of a fix attempt — success or failure.

    Preserves failed attempts so the agent learns from what didn't work,
    not just what did.
    """
    return _emit("attempt_outcome", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "fix_sha": fix_sha,
        "fix_description": fix_description,
        "expected_outcome": expected_outcome,
        "actual_outcome": actual_outcome,
        "what_was_learned": what_was_learned,
        "keep_or_revert": keep_or_revert,
    }, cause_event_id=cause_event_id)


# ── Free-form notes ──────────────────────────────────────────────────

def log_note(
    run_id: str,
    target_host: str,
    rhel_topic: str,
    text: str,
    category: str = "observation",
    attempt_number: int | None = None,
) -> dict:
    return _emit("note", run_id, target_host, rhel_topic, {
        "attempt_number": attempt_number,
        "category": category,
        "text": text,
    })


# ── Queries ───────────────────────────────────────────────────────────

def _load_all() -> list[dict]:
    if not _JOURNAL_PATH.exists():
        return []
    events = []
    for line in _JOURNAL_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            logger.warning("Corrupted JSONL line in run_journal, skipping: %s", e)
            continue
    return events


def get_run_events(run_id: str) -> list[dict]:
    return [e for e in _load_all() if e.get("run_id") == run_id]


def get_run_summary(run_id: str) -> dict:
    events = get_run_events(run_id)
    if not events:
        return {"error": f"No events found for run {run_id}"}

    completed = next((e for e in events if e["event_type"] == "run_completed"), None)
    workflows = [e for e in events if e["event_type"] == "workflow_completed"]
    plans = [e for e in events if e["event_type"] == "plan_recorded"]
    fixes = [e for e in events if e["event_type"] == "fix_applied"]
    reverts = [e for e in events if e["event_type"] == "fix_reverted"]
    diagnoses = [e for e in events if e["event_type"] == "diagnosis_recorded"]

    summary = {
        "run_id": run_id,
        "target_host": events[0].get("target_host", ""),
        "rhel_topic": events[0].get("rhel_topic", ""),
        "started_at": events[0].get("timestamp", ""),
        "total_events": len(events),
        "workflow_runs": len(workflows),
        "diagnoses": len(diagnoses),
        "plans": len(plans),
        "fixes_applied": len(fixes),
        "fixes_reverted": len(reverts),
    }

    if completed:
        d = completed["data"]
        summary["success"] = d.get("success")
        summary["total_attempts"] = d.get("total_attempts")
        summary["total_elapsed_seconds"] = d.get("total_elapsed_seconds")
        summary["final_phase_reached"] = d.get("final_phase_reached")
        summary["pr_url"] = d.get("pr_url")

    timeline = []
    for e in events:
        t = e["event_type"]
        d = e["data"]
        ts = e["timestamp"][11:19]
        if t == "run_started":
            timeline.append(f"[{ts}] Run started on {d.get('hostname', '?')} ({e.get('rhel_topic', '?')})")
        elif t == "workflow_completed":
            status = "SUCCESS" if d.get("success") else f"FAILED at phase {d.get('phase_reached', '?')}"
            timeline.append(f"[{ts}] Attempt {d.get('attempt_number')}: {status} ({d.get('elapsed_seconds', '?')}s)")
        elif t == "plan_recorded":
            timeline.append(f"[{ts}] Plan #{d.get('attempt_number')}: {d.get('root_cause', '?')[:80]} [{d.get('confidence', '?')}]")
        elif t == "fix_applied":
            timeline.append(f"[{ts}] Fix #{d.get('attempt_number')}: {d.get('commit_sha', '?')[:8]} ({d.get('review_result', '?')})")
        elif t == "fix_reverted":
            timeline.append(f"[{ts}] Reverted fix #{d.get('attempt_number')}: {d.get('revert_reason', '?')}")
        elif t == "run_completed":
            result = "SUCCESS" if d.get("success") else "FAILED"
            timeline.append(f"[{ts}] Run completed: {result} after {d.get('total_attempts', '?')} attempts")

    summary["timeline"] = timeline
    return summary


def list_runs(
    target_host: str = "",
    limit: int = 20,
    success_only: bool = False,
    failure_only: bool = False,
) -> list[dict]:
    events = _load_all()
    runs: dict[str, list[dict]] = {}
    for e in events:
        rid = e.get("run_id", "untracked")
        if rid not in runs:
            runs[rid] = []
        runs[rid].append(e)

    results = []
    for run_id, run_events in runs.items():
        if run_id == "untracked":
            continue
        started = next((e for e in run_events if e["event_type"] == "run_started"), None)
        completed = next((e for e in run_events if e["event_type"] == "run_completed"), None)
        if not started:
            continue

        host = started.get("target_host", "")
        if target_host and host != target_host:
            continue

        entry = {
            "run_id": run_id,
            "target_host": host,
            "rhel_topic": started.get("rhel_topic", ""),
            "started_at": started.get("timestamp", ""),
            "event_count": len(run_events),
        }
        if completed:
            d = completed["data"]
            entry["success"] = d.get("success")
            entry["total_attempts"] = d.get("total_attempts")
            entry["total_elapsed_seconds"] = d.get("total_elapsed_seconds")
            entry["failure_category"] = d.get("failure_category")
        else:
            entry["success"] = None
            entry["status"] = "in_progress_or_abandoned"

        if success_only and entry.get("success") is not True:
            continue
        if failure_only and entry.get("success") is not False:
            continue
        results.append(entry)

    results.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return results[:limit]


def get_trends(days: int = 30) -> dict:
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    events = _load_all()
    completed_events = [
        e for e in events
        if e["event_type"] == "run_completed" and e.get("timestamp", "") >= cutoff
    ]

    total = len(completed_events)
    successes = sum(1 for e in completed_events if e["data"].get("success"))
    failures = total - successes

    by_host: dict[str, dict] = {}
    by_topic: dict[str, dict] = {}
    by_category: dict[str, int] = {}
    attempts_list = []
    durations_list = []

    for e in completed_events:
        host = e.get("target_host", "unknown")
        topic = e.get("rhel_topic", "unknown")
        d = e["data"]

        for group, key in [(by_host, host), (by_topic, topic)]:
            if key not in group:
                group[key] = {"total": 0, "success": 0, "failure": 0}
            group[key]["total"] += 1
            if d.get("success"):
                group[key]["success"] += 1
            else:
                group[key]["failure"] += 1

        cat = d.get("failure_category")
        if cat:
            by_category[cat] = by_category.get(cat, 0) + 1

        if d.get("total_attempts"):
            attempts_list.append(d["total_attempts"])
        if d.get("total_elapsed_seconds"):
            durations_list.append(d["total_elapsed_seconds"])

    return {
        "period_days": days,
        "total_runs": total,
        "successes": successes,
        "failures": failures,
        "success_rate": round(successes / total, 2) if total else None,
        "avg_attempts": round(sum(attempts_list) / len(attempts_list), 1) if attempts_list else 0,
        "avg_duration_minutes": round(sum(durations_list) / len(durations_list) / 60, 1) if durations_list else 0,
        "by_host": by_host,
        "by_topic": by_topic,
        "failure_categories": by_category,
    }


def search_diagnoses(
    query: str,
    threshold: float = 0.5,
    max_results: int = 10,
) -> list[dict]:
    events = _load_all()
    searchable = [
        e for e in events
        if e["event_type"] in ("diagnosis_recorded", "plan_recorded")
        and e["data"].get("_embedding")
    ]

    if not searchable:
        query_lower = query.lower()
        results = []
        for e in events:
            if e["event_type"] not in ("diagnosis_recorded", "plan_recorded"):
                continue
            text = json.dumps(e["data"]).lower()
            if query_lower in text:
                result = {k: v for k, v in e.items()}
                result["data"] = {k: v for k, v in e["data"].items() if k != "_embedding"}
                result["_similarity"] = 0.5
                results.append(result)
        return results[:max_results]

    try:
        from .knowledge_base import _embed, _cosine_similarity
        query_emb = _embed(query)
    except Exception:
        return []

    scored = []
    for e in searchable:
        emb = e["data"].get("_embedding", [])
        if not emb:
            continue
        sim = _cosine_similarity(query_emb, emb)
        if sim >= threshold:
            clean_event = {k: v for k, v in e.items()}
            clean_event["data"] = {k: v for k, v in e["data"].items() if k != "_embedding"}
            clean_event["_similarity"] = round(sim, 3)
            scored.append(clean_event)

    scored.sort(key=lambda x: -x["_similarity"])
    return scored[:max_results]


def find_pattern(
    failing_task: str = "",
    error_pattern: str = "",
    phase: int = 0,
    rhel_topic: str = "",
    target_host: str = "",
    days: int = 90,
) -> dict:
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    events = _load_all()
    relevant = [
        e for e in events
        if e["event_type"] in ("triage_started", "workflow_completed")
        and e.get("timestamp", "") >= cutoff
    ]

    matches = []
    for e in relevant:
        d = e["data"]
        if failing_task and failing_task.lower() not in d.get("failing_task", "").lower():
            continue
        if error_pattern and error_pattern.lower() not in (d.get("error_message", "") + d.get("error_summary", "")).lower():
            continue
        if phase and d.get("phase", d.get("phase_reached")) != phase:
            continue
        if rhel_topic and e.get("rhel_topic", "") != rhel_topic:
            continue
        if target_host and e.get("target_host", "") != target_host:
            continue
        matches.append(e)

    run_ids = set(e.get("run_id") for e in matches)

    return {
        "query": {
            "failing_task": failing_task, "error_pattern": error_pattern,
            "phase": phase, "rhel_topic": rhel_topic,
            "target_host": target_host, "days": days,
        },
        "match_count": len(matches),
        "distinct_runs": len(run_ids),
        "first_seen": min((e["timestamp"] for e in matches), default=None),
        "last_seen": max((e["timestamp"] for e in matches), default=None),
        "matches": matches[-10:],
    }


def get_journal_summary() -> str:
    events = _load_all()
    if not events:
        return "No run journal entries yet."

    completed = [e for e in events if e["event_type"] == "run_completed"]
    total = len(completed)
    successes = sum(1 for e in completed if e["data"].get("success"))

    lines = [f"Run Journal: {total} completed runs ({successes} successful, {total - successes} failed)"]

    if total > 0:
        lines.append(f"Overall success rate: {round(successes / total * 100, 1)}%")

    recent = completed[-5:]
    if recent:
        lines.append("\nRecent runs:")
        for e in reversed(recent):
            d = e["data"]
            status = "OK" if d.get("success") else "FAIL"
            host = e.get("target_host", "?").split(".")[0]
            topic = e.get("rhel_topic", "?")
            attempts = d.get("total_attempts", "?")
            dur = d.get("total_elapsed_seconds", 0)
            dur_min = round(dur / 60, 1) if dur else "?"
            ts = e.get("timestamp", "?")[:10]
            lines.append(f"  [{status}] {host}/{topic} — {attempts} attempts, {dur_min}min ({ts})")

    return "\n".join(lines)
