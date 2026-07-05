"""
Pub/Sub usage tracker -- prevents exceeding the free tier (10 GiB/month).

Tracks bytes published and received. Warns at 80% and hard-blocks at 95%.
Usage is persisted to a JSON file so it survives restarts and accumulates
across the month.
"""

import json
import logging
import datetime
import os
from pathlib import Path

logger = logging.getLogger(__name__)

FREE_TIER_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
WARN_THRESHOLD = 0.80   # warn at 80%
BLOCK_THRESHOLD = 0.95  # hard block at 95%

_USAGE_FILE = Path(os.environ.get("DCI_LOG_DIR", "/tmp/dci-agent-logs")) / "pubsub_usage.json"


def _load() -> dict:
    if _USAGE_FILE.exists():
        try:
            data = json.loads(_USAGE_FILE.read_text())
            if data.get("month") == _current_month():
                return data
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Corrupted usage file, resetting: %s", e)
    return {"month": _current_month(), "bytes_published": 0, "bytes_received": 0}


def _save(data: dict):
    _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USAGE_FILE.write_text(json.dumps(data, indent=2))


def _current_month() -> str:
    return datetime.datetime.now().strftime("%Y-%m")


def _total_bytes(data: dict) -> int:
    return data.get("bytes_published", 0) + data.get("bytes_received", 0)


def _usage_pct(data: dict) -> float:
    return _total_bytes(data) / FREE_TIER_BYTES


def check_before_publish(message_bytes: int) -> str | None:
    """
    Check if publishing this message would exceed the free tier.
    Returns an error string if blocked, None if OK.
    Also warns at 80%.
    """
    data = _load()
    projected = _total_bytes(data) + message_bytes
    pct = projected / FREE_TIER_BYTES

    if pct >= BLOCK_THRESHOLD:
        used_mb = _total_bytes(data) / (1024 * 1024)
        limit_mb = FREE_TIER_BYTES / (1024 * 1024)
        logger.critical(
            "PUBSUB USAGE HARD BLOCK: %.1f MB / %.0f MB (%.1f%%). "
            "Refusing to publish to stay within free tier.",
            used_mb, limit_mb, pct * 100,
        )
        return (
            f"BLOCKED: Pub/Sub usage at {pct*100:.1f}% of free tier "
            f"({used_mb:.1f} MB / {limit_mb:.0f} MB). "
            "Publishing stopped to avoid charges. "
            "Reset usage with: rm /tmp/dci-agent-logs/pubsub_usage.json"
        )

    if pct >= WARN_THRESHOLD:
        used_mb = _total_bytes(data) / (1024 * 1024)
        logger.warning(
            "PUBSUB USAGE WARNING: %.1f MB used (%.1f%% of free tier). "
            "Consider reducing verbosity or message frequency.",
            used_mb, pct * 100,
        )

    return None


def record_published(num_bytes: int):
    """Record bytes published to a topic."""
    data = _load()
    data["bytes_published"] = data.get("bytes_published", 0) + num_bytes
    _save(data)


def record_received(num_bytes: int):
    """Record bytes received from a subscription."""
    data = _load()
    data["bytes_received"] = data.get("bytes_received", 0) + num_bytes
    _save(data)


def get_usage_summary() -> dict:
    """Return current usage statistics."""
    data = _load()
    total = _total_bytes(data)
    return {
        "month": data["month"],
        "bytes_published": data.get("bytes_published", 0),
        "bytes_received": data.get("bytes_received", 0),
        "total_bytes": total,
        "total_mb": round(total / (1024 * 1024), 2),
        "free_tier_mb": round(FREE_TIER_BYTES / (1024 * 1024), 0),
        "usage_pct": round(_usage_pct(data) * 100, 4),
        "status": (
            "BLOCKED" if _usage_pct(data) >= BLOCK_THRESHOLD
            else "WARNING" if _usage_pct(data) >= WARN_THRESHOLD
            else "OK"
        ),
    }
