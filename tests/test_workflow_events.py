"""Tests for the workflow events signal file and background poller callbacks."""

import time
from unittest.mock import patch

import pytest

from agents.local import workflow_events
from agents.bridge import pubsub_client as ps

_has_mcp = True
try:
    import mcp as _mcp_mod  # noqa: F401
except ImportError:
    _has_mcp = False


class TestWorkflowEvents:
    """Test the signal file push/pop/has lifecycle."""

    @pytest.fixture(autouse=True)
    def _use_tmp(self, tmp_path):
        self._orig = workflow_events._EVENTS_FILE
        workflow_events._EVENTS_FILE = tmp_path / "workflow_events.json"
        yield
        workflow_events._EVENTS_FILE = self._orig

    def test_empty_initially(self):
        assert workflow_events.has_events() is False
        assert workflow_events.pop_events() == []

    def test_push_and_pop(self):
        workflow_events.push_event({"type": "completed", "target_host": "srv1"})
        workflow_events.push_event({"type": "stuck", "target_host": "srv2"})

        assert workflow_events.has_events() is True
        events = workflow_events.pop_events()
        assert len(events) == 2
        assert events[0]["type"] == "completed"
        assert events[1]["type"] == "stuck"
        assert "timestamp" in events[0]

    def test_pop_drains(self):
        workflow_events.push_event({"type": "completed"})
        workflow_events.pop_events()
        assert workflow_events.has_events() is False
        assert workflow_events.pop_events() == []

    def test_push_preserves_existing(self):
        workflow_events.push_event({"type": "a"})
        workflow_events.push_event({"type": "b"})
        events = workflow_events.pop_events()
        assert len(events) == 2


class TestCallbackRegistry:
    """Test register and invoke callbacks on the background poller."""

    def setup_method(self):
        self._orig_cc = ps._completion_callbacks.copy()
        self._orig_hc = ps._heartbeat_callbacks.copy()
        ps._completion_callbacks.clear()
        ps._heartbeat_callbacks.clear()

    def teardown_method(self):
        ps._completion_callbacks[:] = self._orig_cc
        ps._heartbeat_callbacks[:] = self._orig_hc

    def test_register_completion_callback(self):
        calls = []
        ps.register_completion_callback(lambda cid, r: calls.append((cid, r)))
        assert len(ps._completion_callbacks) == 1

    def test_register_heartbeat_callback(self):
        calls = []
        ps.register_heartbeat_callback(lambda cid, hb: calls.append((cid, hb)))
        assert len(ps._heartbeat_callbacks) == 1


class TestPopPendingCompletions:
    """Test the pop_pending_completions helper."""

    def setup_method(self):
        self._orig = ps._pending_results.copy()
        ps._pending_results.clear()

    def teardown_method(self):
        ps._pending_results.clear()
        ps._pending_results.update(self._orig)

    def test_pops_matching(self):
        ps._pending_results["aaa"] = {"success": True}
        ps._pending_results["bbb"] = {"success": False}
        ps._pending_results["ccc"] = {"success": True}

        found = ps.pop_pending_completions(["aaa", "ccc"])
        assert "aaa" in found
        assert "ccc" in found
        assert found["aaa"]["success"] is True

        assert "aaa" not in ps._pending_results
        assert "bbb" in ps._pending_results

    def test_returns_empty_on_no_match(self):
        ps._pending_results["aaa"] = {"success": True}
        found = ps.pop_pending_completions(["zzz"])
        assert found == {}
        assert "aaa" in ps._pending_results


class TestGetLatestHeartbeat:
    """Test heartbeat cache reads."""

    def setup_method(self):
        self._orig = ps._latest_heartbeats.copy()
        ps._latest_heartbeats.clear()

    def teardown_method(self):
        ps._latest_heartbeats.clear()
        ps._latest_heartbeats.update(self._orig)

    def test_returns_cached(self):
        ps._latest_heartbeats["abc"] = {"phase": "task:Run PBO", "seq": 5}
        result = ps.get_latest_heartbeat("abc")
        assert result["phase"] == "task:Run PBO"

    def test_returns_none_on_miss(self):
        assert ps.get_latest_heartbeat("nonexistent") is None


@pytest.mark.skipif(not _has_mcp, reason="mcp SDK not installed")
class TestOnWorkflowCompleted:
    """Test the MCP server completion callback."""

    @pytest.fixture(autouse=True)
    def _use_tmp(self, tmp_path):
        self._orig = workflow_events._EVENTS_FILE
        workflow_events._EVENTS_FILE = tmp_path / "workflow_events.json"
        yield
        workflow_events._EVENTS_FILE = self._orig

    def test_records_success_event(self):
        from agents.mcp_server import _on_workflow_completed, _corr_to_target, _corr_lock, _inflight_workflows

        with _corr_lock:
            _corr_to_target["test-corr-1"] = "srv1.example.com"
        _inflight_workflows["srv1.example.com"] = {
            "correlation_id": "test-corr-1",
            "start_time": time.time() - 3600,
        }

        with patch("agents.local.fleet_state.record_completion") as mock_record:
            _on_workflow_completed("test-corr-1", {
                "return_code": 0,
                "failure_count": 0,
                "success": True,
            })
            mock_record.assert_called_once()
            args = mock_record.call_args
            assert args[0][0] == "srv1.example.com"
            assert args[0][1] is True

        events = workflow_events.pop_events()
        assert len(events) == 1
        assert events[0]["type"] == "completed"
        assert events[0]["success"] is True

        with _corr_lock:
            _corr_to_target.pop("test-corr-1", None)
        _inflight_workflows.pop("srv1.example.com", None)

    def test_records_failure_event(self):
        from agents.mcp_server import _on_workflow_completed, _corr_to_target, _corr_lock, _inflight_workflows

        with _corr_lock:
            _corr_to_target["test-corr-2"] = "srv2.example.com"
        _inflight_workflows["srv2.example.com"] = {
            "correlation_id": "test-corr-2",
            "start_time": time.time() - 7200,
        }

        with patch("agents.local.fleet_state.record_completion"):
            _on_workflow_completed("test-corr-2", {
                "return_code": 1,
                "failure_count": 1,
                "failures": [{"task": "sap_preconfigure"}],
            })

        events = workflow_events.pop_events()
        assert len(events) == 1
        assert events[0]["success"] is False
        assert len(events[0]["failures"]) == 1

        with _corr_lock:
            _corr_to_target.pop("test-corr-2", None)
        _inflight_workflows.pop("srv2.example.com", None)

    def test_ignores_unknown_correlation(self):
        from agents.mcp_server import _on_workflow_completed
        _on_workflow_completed("unknown-corr", {"success": True})
        assert workflow_events.has_events() is False


@pytest.mark.skipif(not _has_mcp, reason="mcp SDK not installed")
class TestOnHeartbeat:
    """Test stuck phase detection in the heartbeat callback."""

    @pytest.fixture(autouse=True)
    def _use_tmp(self, tmp_path):
        self._orig = workflow_events._EVENTS_FILE
        workflow_events._EVENTS_FILE = tmp_path / "workflow_events.json"
        yield
        workflow_events._EVENTS_FILE = self._orig

    def setup_method(self):
        from agents import mcp_server
        self._orig_starts = mcp_server._phase_start_times.copy()
        self._orig_alerts = mcp_server._stuck_alerted.copy()
        mcp_server._phase_start_times.clear()
        mcp_server._stuck_alerted.clear()

    def teardown_method(self):
        from agents import mcp_server
        mcp_server._phase_start_times.clear()
        mcp_server._phase_start_times.update(self._orig_starts)
        mcp_server._stuck_alerted.clear()
        mcp_server._stuck_alerted.update(self._orig_alerts)

    def test_detects_stuck_phase(self):
        from agents.mcp_server import _on_heartbeat, _corr_to_target, _corr_lock, _phase_start_times

        with _corr_lock:
            _corr_to_target["hb-corr-1"] = "stuck-srv.example.com"
        _phase_start_times["stuck-srv.example.com"] = {
            4: time.time() - 6000,  # 100 minutes ago — phase 4 max is 90
        }

        _on_heartbeat("hb-corr-1", {"phase": "task:Run PBO", "seq": 1})

        events = workflow_events.pop_events()
        assert len(events) == 1
        assert events[0]["type"] == "stuck"
        assert events[0]["phase"] == 4

        with _corr_lock:
            _corr_to_target.pop("hb-corr-1", None)

    def test_stuck_alert_fires_once(self):
        from agents.mcp_server import _on_heartbeat, _corr_to_target, _corr_lock, _phase_start_times

        with _corr_lock:
            _corr_to_target["hb-corr-2"] = "once-srv.example.com"
        _phase_start_times["once-srv.example.com"] = {
            4: time.time() - 6000,
        }

        _on_heartbeat("hb-corr-2", {"phase": "task:Run PBO", "seq": 1})
        _on_heartbeat("hb-corr-2", {"phase": "task:Run PBO", "seq": 2})
        _on_heartbeat("hb-corr-2", {"phase": "task:Run PBO", "seq": 3})

        events = workflow_events.pop_events()
        assert len(events) == 1

        with _corr_lock:
            _corr_to_target.pop("hb-corr-2", None)

    def test_no_alert_when_on_time(self):
        from agents.mcp_server import _on_heartbeat, _corr_to_target, _corr_lock, _phase_start_times

        with _corr_lock:
            _corr_to_target["hb-corr-3"] = "ok-srv.example.com"
        _phase_start_times["ok-srv.example.com"] = {
            4: time.time() - 60,  # 1 minute — well within limits
        }

        _on_heartbeat("hb-corr-3", {"phase": "task:Run PBO", "seq": 1})

        assert workflow_events.has_events() is False

        with _corr_lock:
            _corr_to_target.pop("hb-corr-3", None)
