"""
Fleet state manager for N concurrent DCI workflows.

Persists fleet goals (nr counters), completion history, and monitor health
to fleet_state.json. Atomic writes via filelock.py. Any session can read
the file to see the current state -- the relay tracks what's alive, this
file tracks what we want to achieve.
"""

import json
import time
from pathlib import Path

from .filelock import atomic_write_json

_STATE_FILE = Path(__file__).parent / "fleet_state.json"
_MAX_COMPLETIONS = 100


def _load() -> dict:
    """Load fleet state from disk, or return empty state."""
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return _empty_state()
    return _empty_state()


def _empty_state() -> dict:
    return {
        "goals": {},
        "last_poll_time": 0.0,
        "completions": [],
    }


def _save(state: dict):
    """Save fleet state to disk atomically."""
    if len(state.get("completions", [])) > _MAX_COMPLETIONS:
        state["completions"] = state["completions"][-_MAX_COMPLETIONS:]
    atomic_write_json(_STATE_FILE, state)


def set_goal(target: str, nr: int, topic: str):
    """Set or update the fleet goal for a target server."""
    state = _load()
    existing = state["goals"].get(target, {})
    state["goals"][target] = {
        "nr_target": nr,
        "nr_completed": existing.get("nr_completed", 0),
        "nr_failed": existing.get("nr_failed", 0),
        "topic": topic,
    }
    _save(state)


def record_completion(target: str, success: bool, elapsed: int):
    """Record a workflow completion and update counters."""
    state = _load()
    goal = state["goals"].get(target)
    if goal:
        if success:
            goal["nr_completed"] = goal.get("nr_completed", 0) + 1
        else:
            goal["nr_failed"] = goal.get("nr_failed", 0) + 1
    state["completions"].append({
        "target": target,
        "success": success,
        "elapsed": elapsed,
        "timestamp": time.time(),
    })
    _save(state)


def get_goals() -> dict:
    """Return all fleet goals with current counters."""
    state = _load()
    return state.get("goals", {})


def should_redispatch(target: str) -> bool:
    """Check if a target needs another run (nr_completed < nr_target)."""
    state = _load()
    goal = state["goals"].get(target)
    if not goal:
        return False
    return goal.get("nr_completed", 0) < goal.get("nr_target", 1)


def mark_done(target: str):
    """Mark a target as done (nr satisfied or manually stopped)."""
    state = _load()
    goal = state["goals"].get(target)
    if goal:
        goal["nr_target"] = goal.get("nr_completed", 0)
    _save(state)


def mark_failed(target: str):
    """Mark a target as permanently failed (all fix attempts exhausted)."""
    state = _load()
    goal = state["goals"].get(target)
    if goal:
        goal["nr_target"] = -1
    _save(state)


def update_poll_time():
    """Record that the monitor polled successfully."""
    state = _load()
    state["last_poll_time"] = time.time()
    _save(state)


def get_last_poll_time() -> float:
    """Return the timestamp of the last monitor poll."""
    state = _load()
    return state.get("last_poll_time", 0.0)


def get_completions(limit: int = 20) -> list:
    """Return recent completions."""
    state = _load()
    return state.get("completions", [])[-limit:]


def clear_goals():
    """Remove all fleet goals (for cleanup after all runs complete)."""
    state = _load()
    state["goals"] = {}
    _save(state)


def remove_goal(target: str):
    """Remove a specific target's goal."""
    state = _load()
    state["goals"].pop(target, None)
    _save(state)
