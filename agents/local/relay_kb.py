"""
Relay / Pub/Sub knowledge base — tracks infrastructure issues and resolutions.

Same model as knowledge_base.py but focused on the relay-to-jumpbox chain:
Pub/Sub connectivity, SSH connections, credential problems, relay daemon
health, and MCP tool failures. Auto-populated by the MCP server layer.
"""

import json
import datetime
import logging

from .. import config

logger = logging.getLogger(__name__)

_RELAY_KB_PATH = config.LOG_DIR / "relay_kb.json"


RELAY_ISSUE_CATEGORIES = {
    "pubsub_credentials": [
        "403", "SERVICE_DISABLED", "credentials rejected", "sa key",
        "service account", "permission denied", "iam", "wrong project",
    ],
    "pubsub_connectivity": [
        "timeout", "DEADLINE_EXCEEDED", "504", "pubsub",
        "publish failed", "pull error", "network", "unreachable",
    ],
    "relay_daemon": [
        "relay daemon", "not responding", "daemon stopped", "daemon crashed",
        "relay update", "git pull", "restart", "process",
    ],
    "ssh_jumpbox": [
        "jumpbox", "ssh", "paramiko", "connection refused",
        "host key", "authentication", "transport",
    ],
    "ssh_target": [
        "target", "two-hop", "channel", "open_channel",
        "password", "auth failed", "connection reset",
    ],
    "concurrency": [
        "lock", "thread", "executor", "max_workers", "blocked",
        "stuck", "deadlock", "saturated", "concurrent",
    ],
    "mcp_server": [
        "mcp", "stdio", "tool call", "user-cancel",
        "mcp error", "-32001", "process died",
    ],
    "settings_sync": [
        "settings sync", "sync_settings", "configure_target",
        "disk_map", "settings file", "run_config",
    ],
}


def classify_relay_issue(error: str, diagnosis: str = "") -> str:
    combined = f"{error} {diagnosis}".lower()
    scores = {}
    for category, keywords in RELAY_ISSUE_CATEGORIES.items():
        score = sum(1 for kw in keywords if kw.lower() in combined)
        if score > 0:
            scores[category] = score
    if scores:
        return max(scores, key=scores.get)
    return "unknown"


def _load() -> list[dict]:
    if _RELAY_KB_PATH.exists():
        try:
            return json.loads(_RELAY_KB_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Corrupted relay_kb.json, returning empty: %s", e)
            return []
    return []


def _save(entries: list[dict]) -> None:
    from .filelock import atomic_write_json
    atomic_write_json(_RELAY_KB_PATH, entries)


def record_relay_issue(
    error: str,
    diagnosis: str,
    resolution: str,
    resolved: bool,
    tool_name: str = "",
    diagnostics: list[str] | None = None,
    health_check: list[str] | None = None,
) -> dict:
    """Record a relay/Pub/Sub issue and its resolution.

    Args:
        error: The error message or symptom.
        diagnosis: Root cause analysis.
        resolution: What fixed it (or what was attempted).
        resolved: Whether the issue is now fixed.
        tool_name: Which MCP tool triggered the issue.
        diagnostics: The _diagnostics list from the tool response.
        health_check: The _health_check details from preflight.
    """
    entries = _load()
    category = classify_relay_issue(error, diagnosis)

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "error": error,
        "diagnosis": diagnosis,
        "resolution": resolution,
        "resolved": resolved,
        "category": category,
        "tool_name": tool_name,
        "diagnostics": diagnostics or [],
        "health_check": health_check or [],
    }
    entries.append(entry)
    _save(entries)

    try:
        from .events import emit as unified_emit
        unified_emit(
            "relay.issue_recorded",
            data={"error": error, "diagnosis": diagnosis,
                  "resolution": resolution, "resolved": resolved,
                  "category": category, "tool_name": tool_name},
            root_cause_category=category,
        )
    except Exception as e:
        logger.warning("Unified event forward failed in relay_kb: %s", e)

    return {
        "success": True,
        "message": f"Relay issue recorded ({category}).",
        "total_entries": len(entries),
    }


def search_relay_issues(query: str, max_results: int = 10) -> dict:
    """Search relay KB by substring matching."""
    entries = _load()
    if not entries:
        return {"query": query, "match_count": 0, "matches": []}

    query_lower = query.lower()
    matches = []
    for entry in entries:
        searchable = f"{entry.get('error', '')} {entry.get('diagnosis', '')} {entry.get('resolution', '')}".lower()
        if query_lower in searchable:
            matches.append(entry)

    matches.sort(key=lambda e: (not e.get("resolved", False), e.get("timestamp", "")), reverse=True)

    return {
        "query": query,
        "match_count": len(matches[:max_results]),
        "matches": matches[:max_results],
    }


def get_relay_stats() -> dict:
    """Per-category stats for relay issues."""
    entries = _load()
    if not entries:
        return {"total": 0, "categories": {}}

    stats = {}
    for entry in entries:
        cat = entry.get("category", "unknown")
        if cat not in stats:
            stats[cat] = {"total": 0, "resolved": 0, "unresolved": 0}
        stats[cat]["total"] += 1
        if entry.get("resolved", False):
            stats[cat]["resolved"] += 1
        else:
            stats[cat]["unresolved"] += 1

    for cat in stats:
        t = stats[cat]["total"]
        stats[cat]["resolution_rate"] = round(stats[cat]["resolved"] / t * 100, 1) if t > 0 else 0.0

    return {"total": len(entries), "categories": stats}


def get_relay_summary() -> str:
    """Human-readable summary of relay issue history."""
    entries = _load()
    if not entries:
        return "No relay issues recorded yet."

    stats = get_relay_stats()
    lines = [f"Relay KB: {stats['total']} issue(s) recorded"]
    for cat, s in sorted(stats["categories"].items(), key=lambda x: -x[1]["total"]):
        lines.append(
            f"  {cat}: {s['total']} total, {s['resolved']} resolved, "
            f"{s['unresolved']} unresolved ({s['resolution_rate']}% resolution rate)"
        )

    recent = entries[-3:]
    lines.append("\nRecent issues:")
    for e in reversed(recent):
        status = "RESOLVED" if e.get("resolved") else "OPEN"
        lines.append(f"  [{status}] {e.get('category', '?')}: {e.get('error', '?')[:80]}")

    return "\n".join(lines)
