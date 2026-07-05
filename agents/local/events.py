"""
Unified event log — one JSONL file, one schema for everything.

Addresses the "7 stores, 7 schemas" problem by providing a single
append-only event stream that receives copies from all existing stores.
Each event carries causal links, normalized errors, fix pattern tags,
and environment context for cross-cutting queries.

The existing stores (KB, journal, relay KB, audit) keep working unchanged.
This module is additive — it never replaces their writes, only mirrors them.
"""

import datetime
import hashlib
import json
import logging
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_DIR = Path("/tmp/dci-agent-logs")
try:
    from .. import config
    _LOG_DIR = config.LOG_DIR
except Exception:
    pass

_EVENTS_PATH = _LOG_DIR / "events.jsonl"

# ── ANSI / noise stripping (Ng) ─────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_PATH_HOME_RE = re.compile(r"/home/\w+/")
_PATH_TMP_RE = re.compile(r"/tmp/[a-zA-Z0-9._-]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_error(raw: str) -> str:
    """Strip ANSI codes, mask timestamps/paths, collapse whitespace."""
    text = _ANSI_RE.sub("", raw)
    text = _TIMESTAMP_RE.sub("<TIMESTAMP>", text)
    text = _PATH_HOME_RE.sub("/home/<USER>/", text)
    text = _PATH_TMP_RE.sub("<TMPPATH>", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:2000]


def error_signature(text: str) -> str:
    """SHA-256 prefix of normalized error for dedup."""
    normalized = normalize_error(text)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ── Core emit ────────────────────────────────────────────────────────

def emit(
    event_type: str,
    *,
    run_id: str = "",
    target_host: str = "",
    rhel_topic: str = "",
    attempt_number: int = 0,
    phase: int = 0,
    data: dict | None = None,
    cause_event_id: str = "",
    decision_rationale: str = "",
    root_cause_category: str = "",
    environment_context: dict | None = None,
    fix_pattern: str = "",
    elapsed_seconds: int = 0,
    normalized_error: str = "",
    error_sig: str = "",
) -> dict:
    """Write a universal event to events.jsonl.

    Returns the event dict (including generated event_id).
    """
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now().isoformat(),
        "event_type": event_type,
        "run_id": run_id,
        "target_host": target_host,
        "rhel_topic": rhel_topic,
        "attempt_number": attempt_number,
        "phase": phase,
        "data": data or {},
        "cause_event_id": cause_event_id,
        "decision_rationale": decision_rationale,
        "root_cause_category": root_cause_category,
        "environment_context": environment_context or {},
        "fix_pattern": fix_pattern,
        "elapsed_seconds": elapsed_seconds,
        "normalized_error": normalized_error,
        "error_signature": error_sig,
    }

    try:
        from .filelock import locked_append
        locked_append(_EVENTS_PATH, json.dumps(event, default=str))
    except Exception as e:
        logger.warning("Failed to write unified event: %s", e)

    return event


# ── Queries ──────────────────────────────────────────────────────────

def _load_all() -> list[dict]:
    if not _EVENTS_PATH.exists():
        return []
    events = []
    for line in _EVENTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            logger.warning("Corrupted JSONL line in events log, skipping: %s", e)
            continue
    return events


def search(
    query: str,
    event_types: list[str] | None = None,
    threshold: float = 0.5,
    max_results: int = 20,
) -> list[dict]:
    """Semantic search across the unified event log.

    Falls back to substring matching if embeddings are unavailable.
    """
    events = _load_all()
    if event_types:
        events = [e for e in events if e.get("event_type") in event_types]

    try:
        from .knowledge_base import _embed, _cosine_similarity
        query_emb = _embed(query)
        use_embeddings = True
    except Exception:
        use_embeddings = False

    scored = []
    query_lower = query.lower()

    for e in events:
        text = json.dumps(e.get("data", {}), default=str)

        if use_embeddings:
            emb = e.get("data", {}).get("_embedding")
            if emb:
                sim = _cosine_similarity(query_emb, emb)
                if sim >= threshold:
                    result = _strip_embeddings(e)
                    result["_similarity"] = round(sim, 3)
                    scored.append(result)
                    continue

        if query_lower in text.lower() or query_lower in e.get("normalized_error", "").lower():
            result = _strip_embeddings(e)
            result["_similarity"] = 0.5
            scored.append(result)

    scored.sort(key=lambda x: -x.get("_similarity", 0))
    return scored[:max_results]


def get_decision_metrics(
    category: str = "",
    fix_pattern: str = "",
    days: int = 90,
) -> dict:
    """Compute success_rate x cost per fix strategy.

    Groups events by root_cause_category or fix_pattern, computes
    success rate and average elapsed time for each group.
    """
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    events = _load_all()

    fix_events = [
        e for e in events
        if e.get("event_type") in ("fix.applied", "fix_applied", "kb.fix_recorded")
        and e.get("timestamp", "") >= cutoff
    ]

    if category:
        fix_events = [e for e in fix_events if e.get("root_cause_category") == category]
    if fix_pattern:
        fix_events = [e for e in fix_events if e.get("fix_pattern") == fix_pattern]

    groups: dict[str, dict] = {}

    for e in fix_events:
        key = e.get("fix_pattern") or e.get("root_cause_category") or "unknown"
        if key not in groups:
            groups[key] = {"total": 0, "success": 0, "elapsed_total": 0}
        g = groups[key]
        g["total"] += 1
        if e.get("data", {}).get("success"):
            g["success"] += 1
        g["elapsed_total"] += e.get("elapsed_seconds", 0)

    metrics = {}
    for key, g in groups.items():
        rate = round(g["success"] / g["total"], 2) if g["total"] else 0
        avg_cost = round(g["elapsed_total"] / g["total"]) if g["total"] else 0
        metrics[key] = {
            "total": g["total"],
            "success": g["success"],
            "success_rate": rate,
            "avg_elapsed_seconds": avg_cost,
            "expected_value": round(rate * (1 - avg_cost / 3600), 3) if avg_cost else rate,
        }

    return {
        "period_days": days,
        "filter": {"category": category, "fix_pattern": fix_pattern},
        "strategies": metrics,
    }


def get_causal_chain(event_id: str) -> list[dict]:
    """Trace backward through cause_event_ids.

    Returns the chain from the given event back to the root cause,
    ordered root-first.
    """
    events = _load_all()
    by_id = {e["event_id"]: e for e in events if "event_id" in e}

    chain = []
    current_id = event_id
    seen = set()

    while current_id and current_id not in seen:
        seen.add(current_id)
        event = by_id.get(current_id)
        if not event:
            break
        chain.append(_strip_embeddings(event))
        current_id = event.get("cause_event_id", "")

    chain.reverse()
    return chain


def get_events_for_run(run_id: str) -> list[dict]:
    """Get all unified events for a specific run."""
    return [_strip_embeddings(e) for e in _load_all() if e.get("run_id") == run_id]


def get_event_counts(days: int = 30) -> dict:
    """Summary counts by event_type for the last N days."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    events = _load_all()
    counts: dict[str, int] = {}
    for e in events:
        if e.get("timestamp", "") >= cutoff:
            t = e.get("event_type", "unknown")
            counts[t] = counts.get(t, 0) + 1
    return {"period_days": days, "counts": counts, "total": sum(counts.values())}


# ── Helpers ──────────────────────────────────────────────────────────

def _strip_embeddings(event: dict) -> dict:
    """Remove _embedding fields from event for display."""
    clean = dict(event)
    if "data" in clean and isinstance(clean["data"], dict):
        clean["data"] = {k: v for k, v in clean["data"].items() if k != "_embedding"}
    return clean
