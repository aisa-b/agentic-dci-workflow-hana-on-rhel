"""
Signal file for workflow events detected by the background Pub/Sub poller.

When the poller detects a completion or stuck phase, it writes a JSON event
here. The cron poll or dci_check_events tool reads and drains the events,
then triggers the appropriate action (triage, re-dispatch, investigation).
"""

import json
import time
from pathlib import Path

from .filelock import atomic_write_json

_EVENTS_FILE = Path(__file__).parent / "workflow_events.json"


def _load() -> dict:
    if _EVENTS_FILE.exists():
        try:
            return json.loads(_EVENTS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"pending_events": []}
    return {"pending_events": []}


def push_event(event: dict):
    """Append a workflow event to the signal file."""
    event.setdefault("timestamp", time.time())
    state = _load()
    state["pending_events"].append(event)
    atomic_write_json(_EVENTS_FILE, state)


def pop_events() -> list[dict]:
    """Return and clear all pending events."""
    state = _load()
    events = state.get("pending_events", [])
    if events:
        state["pending_events"] = []
        atomic_write_json(_EVENTS_FILE, state)
    return events


def has_events() -> bool:
    """Quick check for pending events without loading the full file."""
    state = _load()
    return bool(state.get("pending_events"))
