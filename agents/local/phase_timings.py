"""
Phase timing tracker — records per-phase durations after each workflow run.

Builds up historical data so the agent can answer "how long does PBO take?"
and detect anomalies (e.g., a phase taking 3x longer than average).

Data stored in phase_timings.jsonl (one JSON object per completed run).
"""

import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path

from .filelock import locked_append

logger = logging.getLogger(__name__)

_TIMINGS_FILE = Path(__file__).resolve().parent.parent.parent / "phase_timings.jsonl"


def record_run(
    target_host: str,
    topic: str,
    total_seconds: int,
    success: bool,
    phase_timings: dict[str, int] | None = None,
    fix_attempts: int = 0,
    phases_reached: int = 5,
    server_profile: dict | None = None,
) -> dict:
    """Record timing data for a completed workflow run.

    server_profile should include hardware info (vendor, model, memory_gb,
    cpu_count) so timings can be compared across server types.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": target_host,
        "topic": topic,
        "total_seconds": total_seconds,
        "success": success,
        "fix_attempts": fix_attempts,
        "phases_reached": phases_reached,
    }
    if phase_timings:
        entry["phases"] = phase_timings
    if server_profile:
        entry["hardware"] = {
            k: server_profile[k]
            for k in ("vendor", "model", "memory_gb", "cpu_count", "rhel_version", "kernel")
            if k in server_profile
        }

    locked_append(_TIMINGS_FILE, entry)
    logger.info("Recorded run timing: %s %ds success=%s", target_host, total_seconds, success)
    return entry


def get_history(target: str = "", topic: str = "", limit: int = 50) -> list[dict]:
    """Read historical run timings, optionally filtered by target/topic."""
    if not _TIMINGS_FILE.exists():
        return []

    entries = []
    for line in _TIMINGS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if target and entry.get("target") != target:
                continue
            if topic and entry.get("topic") != topic:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    return entries[-limit:]


def get_stats(phase: str = "", target: str = "", topic: str = "", vendor: str = "") -> dict:
    """Compute statistics for total runtime or a specific phase.

    Filter by target, topic, or hardware vendor for meaningful comparisons
    across different server types (HPE vs Lenovo).

    Returns dict with count, mean, median, min, max in seconds.
    """
    history = get_history(target=target, topic=topic)
    successful = [e for e in history if e.get("success")]
    if vendor:
        successful = [
            e for e in successful
            if e.get("hardware", {}).get("vendor", "").lower() == vendor.lower()
        ]

    if not successful:
        return {"count": 0, "message": "No successful runs recorded yet"}

    if phase:
        values = [
            e["phases"][phase]
            for e in successful
            if e.get("phases", {}).get(phase) is not None
        ]
        label = phase
    else:
        values = [e["total_seconds"] for e in successful]
        label = "total_runtime"

    if not values:
        return {"count": 0, "message": f"No data for phase '{phase}'"}

    return {
        "metric": label,
        "count": len(values),
        "mean": round(statistics.mean(values)),
        "median": round(statistics.median(values)),
        "min": min(values),
        "max": max(values),
        "stdev": round(statistics.stdev(values)) if len(values) > 1 else 0,
        "unit": "seconds",
    }


def format_stats(stats: dict) -> str:
    """Human-readable stats summary."""
    if stats.get("count", 0) == 0:
        return stats.get("message", "No data")

    mean_m = stats["mean"] // 60
    median_m = stats["median"] // 60
    min_m = stats["min"] // 60
    max_m = stats["max"] // 60

    return (
        f"{stats['metric']}: {stats['count']} runs — "
        f"mean {mean_m}m, median {median_m}m, "
        f"range {min_m}m–{max_m}m"
    )
