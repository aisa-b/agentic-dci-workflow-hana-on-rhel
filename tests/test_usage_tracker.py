"""Tests for agents/bridge/usage_tracker.py — free tier tracking and limits."""

import json
from unittest.mock import patch

import pytest

from agents.bridge import usage_tracker


@pytest.fixture(autouse=True)
def isolated_usage_file(tmp_path):
    path = tmp_path / "pubsub_usage.json"
    with patch.object(usage_tracker, "_USAGE_FILE", path):
        yield path


class TestUsageTracking:
    def test_record_published_accumulates(self):
        usage_tracker.record_published(1000)
        usage_tracker.record_published(2000)
        summary = usage_tracker.get_usage_summary()
        assert summary["bytes_published"] == 3000

    def test_record_received_accumulates(self):
        usage_tracker.record_received(500)
        usage_tracker.record_received(1500)
        summary = usage_tracker.get_usage_summary()
        assert summary["bytes_received"] == 2000

    def test_total_bytes_is_sum(self):
        usage_tracker.record_published(1000)
        usage_tracker.record_received(2000)
        summary = usage_tracker.get_usage_summary()
        assert summary["total_bytes"] == 3000

    def test_usage_pct_calculated(self):
        summary = usage_tracker.get_usage_summary()
        assert summary["usage_pct"] == 0.0
        assert summary["status"] == "OK"


class TestFreeTierLimits:
    def test_allows_small_publish(self):
        assert usage_tracker.check_before_publish(100) is None

    def test_blocks_at_95_percent(self):
        near_limit = int(usage_tracker.FREE_TIER_BYTES * 0.96)
        usage_tracker.record_published(near_limit)
        result = usage_tracker.check_before_publish(100)
        assert result is not None
        assert "BLOCKED" in result

    def test_warns_at_80_percent(self):
        near_warn = int(usage_tracker.FREE_TIER_BYTES * 0.81)
        usage_tracker.record_published(near_warn)
        result = usage_tracker.check_before_publish(100)
        assert result is None

    def test_status_warning_at_80_percent(self):
        near_warn = int(usage_tracker.FREE_TIER_BYTES * 0.81)
        usage_tracker.record_published(near_warn)
        summary = usage_tracker.get_usage_summary()
        assert summary["status"] == "WARNING"

    def test_status_blocked_at_95_percent(self):
        near_limit = int(usage_tracker.FREE_TIER_BYTES * 0.96)
        usage_tracker.record_published(near_limit)
        summary = usage_tracker.get_usage_summary()
        assert summary["status"] == "BLOCKED"


class TestMonthRollover:
    def test_resets_on_new_month(self, isolated_usage_file):
        usage_tracker.record_published(5000)
        data = json.loads(isolated_usage_file.read_text())
        data["month"] = "1999-01"
        isolated_usage_file.write_text(json.dumps(data))

        summary = usage_tracker.get_usage_summary()
        assert summary["total_bytes"] == 0

    def test_handles_corrupted_file(self, isolated_usage_file):
        isolated_usage_file.write_text("not valid json {{}")
        summary = usage_tracker.get_usage_summary()
        assert summary["total_bytes"] == 0
