"""Tests for agents/mcp_server.py — MCP tool functions with mocked Pub/Sub bridge."""

import asyncio
import json
import sys
import time
import types
from unittest.mock import AsyncMock, patch

import pytest

# Mock the mcp module before importing mcp_server
_mock_mcp = types.ModuleType("mcp")
_mock_server = types.ModuleType("mcp.server")
_mock_fastmcp = types.ModuleType("mcp.server.fastmcp")

class _FakeFastMCP:
    def __init__(self, *a, **kw):
        pass
    def tool(self, **kw):
        def decorator(fn):
            return fn
        return decorator

_mock_fastmcp.FastMCP = _FakeFastMCP
sys.modules.setdefault("mcp", _mock_mcp)
sys.modules.setdefault("mcp.server", _mock_server)
sys.modules.setdefault("mcp.server.fastmcp", _mock_fastmcp)

import agents.mcp_server as mcp_server


@pytest.fixture(autouse=True)
def _reset_inflight():
    """Clear inflight state before each test."""
    mcp_server._inflight_workflows.clear()
    mcp_server._corr_to_target.clear()
    yield
    mcp_server._inflight_workflows.clear()
    mcp_server._corr_to_target.clear()


def _run(coro):
    """Run an async function synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# dci_workflow_run
# ---------------------------------------------------------------------------

class TestWorkflowRun:
    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server._resolve_target")
    @patch("agents.mcp_server.bridge")
    @patch("agents.mcp_server._persist_inflight")
    @patch("subprocess.run")
    @patch("tools.sync_hooks.sync_hooks", return_value={"success": True, "status": "clean", "pushed": False, "message": "ok", "hooks_dir": ""})
    def test_successful_start(self, mock_sync, mock_subproc, mock_persist, mock_bridge, mock_resolve, mock_pre):
        mock_resolve.return_value = {
            "success": True,
            "target_host": "target-1.example.com",
            "settings_file": "/etc/dci-rhel-agent/settings_current_target-1.yml",
            "topic": "RHEL-10.2",
        }
        mock_subproc.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
        mock_bridge.send_command_start = AsyncMock(return_value={
            "correlation_id": "abc-123",
            "ack_received": True,
            "success": True,
            "_diagnostics": [],
        })

        result = json.loads(_run(mcp_server.dci_workflow_run(
            target_host="target-1.example.com", topic="RHEL-10.2",
        )))

        assert result["success"] is True
        assert result["started"] is True
        assert result["correlation_id"] == "abc-123"
        assert "target-1.example.com" in mcp_server._inflight_workflows
        assert mcp_server._corr_to_target["abc-123"] == "target-1.example.com"
        mock_persist.assert_called_once()

    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server._resolve_target")
    @patch("agents.mcp_server.bridge")
    def test_duplicate_detection(self, mock_bridge, mock_resolve, mock_pre):
        mock_resolve.return_value = {
            "success": True,
            "target_host": "target-1.example.com",
            "settings_file": "/etc/dci-rhel-agent/settings_current_target-1.yml",
            "topic": "RHEL-10.2",
        }
        mcp_server._inflight_workflows["target-1.example.com"] = {
            "correlation_id": "existing-123",
            "start_time": time.time() - 300,
        }

        result = json.loads(_run(mcp_server.dci_workflow_run(
            target_host="target-1.example.com",
        )))

        assert result["success"] is False
        assert "already running" in result["error"]
        mock_bridge.send_command_start.assert_not_called()

    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server._resolve_target")
    @patch("agents.mcp_server.bridge")
    @patch("tools.sync_hooks.sync_hooks", return_value={"success": True, "status": "clean", "pushed": False, "message": "ok", "hooks_dir": ""})
    def test_no_ack_returns_failure(self, mock_sync, mock_bridge, mock_resolve, mock_pre):
        mock_resolve.return_value = {
            "success": True,
            "target_host": "target-1.example.com",
            "settings_file": "/etc/dci-rhel-agent/settings_current_target-1.yml",
            "topic": "",
        }
        mock_bridge.send_command_start = AsyncMock(return_value={
            "correlation_id": "abc-123",
            "ack_received": False,
            "success": False,
            "error": "No ACK from relay after 60s",
            "_diagnostics": ["timeout"],
        })

        result = json.loads(_run(mcp_server.dci_workflow_run(
            target_host="target-1.example.com",
        )))

        assert result["success"] is False
        assert "No ACK" in result["error"]
        assert "target-1.example.com" not in mcp_server._inflight_workflows

    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server._resolve_target")
    def test_resolve_failure(self, mock_resolve, mock_pre):
        mock_resolve.return_value = {
            "success": False,
            "error": "No target_host provided",
        }

        result = json.loads(_run(mcp_server.dci_workflow_run()))

        assert result["success"] is False
        assert "No target_host" in result["error"]


# ---------------------------------------------------------------------------
# dci_workflow_status
# ---------------------------------------------------------------------------

class TestWorkflowStatus:
    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server.bridge")
    def test_returns_running_with_heartbeat(self, mock_bridge, mock_pre):
        mock_bridge.check_for_result = AsyncMock(return_value={"status": "running"})
        mcp_server._inflight_workflows["target-1.example.com"] = {
            "correlation_id": "abc-123",
            "target_host": "target-1.example.com",
            "start_time": time.time() - 600,
            "last_heartbeat": {"phase": "task:sap-preconfigure", "seq": 5},
            "last_heartbeat_time": time.time(),
            "status": "running",
            "result": None,
        }

        result = json.loads(_run(mcp_server.dci_workflow_status(
            target_host="target-1.example.com",
        )))

        assert result["success"] is True
        assert result["status"] == "running"

    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server.bridge")
    def test_not_found_queries_relay(self, mock_bridge, mock_pre):
        mock_bridge.send_command = AsyncMock(return_value={
            "success": True,
            "workflows": [],
            "completed": [],
        })

        result = json.loads(_run(mcp_server.dci_workflow_status(
            target_host="target-1.example.com",
        )))

        assert result["success"] is False
        assert "No workflow found" in result["error"]

    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server._persist_inflight")
    @patch("agents.mcp_server.bridge")
    def test_auto_registers_from_relay(self, mock_bridge, mock_persist, mock_pre):
        mock_bridge.send_command = AsyncMock(return_value={
            "success": True,
            "workflows": [{
                "target_host": "target-1.example.com",
                "correlation_id": "relay-corr-456",
                "settings_file": "/etc/dci-rhel-agent/settings_current_target-1.yml",
                "running_seconds": 1200,
                "last_phase": "task:hdblcm",
                "last_output_line": "Installing HANA...",
                "last_heartbeat_age": 10,
            }],
            "completed": [],
        })

        result = json.loads(_run(mcp_server.dci_workflow_status(
            target_host="target-1.example.com",
        )))

        assert result["success"] is True
        assert result["status"] == "running"
        assert "target-1.example.com" in mcp_server._inflight_workflows
        assert mcp_server._corr_to_target.get("relay-corr-456") == "target-1.example.com"


# ---------------------------------------------------------------------------
# dci_workflow_list
# ---------------------------------------------------------------------------

class TestWorkflowList:
    @patch("agents.mcp_server._preflight", new_callable=AsyncMock, return_value=None)
    @patch("agents.mcp_server.bridge")
    def test_returns_relay_response(self, mock_bridge, mock_pre):
        relay_response = {
            "success": True,
            "count": 1,
            "workflows": [{"target_host": "target-1.example.com", "running_seconds": 600}],
            "completed": [],
        }
        mock_bridge.send_command = AsyncMock(return_value=relay_response)

        result = json.loads(_run(mcp_server.dci_workflow_list()))

        assert result["success"] is True
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# Inflight state management
# ---------------------------------------------------------------------------

class TestInflightState:
    def test_persist_and_restore(self, tmp_path):
        state_file = tmp_path / "inflight.json"
        with patch("agents.mcp_server._INFLIGHT_FILE", state_file):
            mcp_server._inflight_workflows["target-1.example.com"] = {
                "correlation_id": "abc-123",
                "target_host": "target-1.example.com",
                "start_time": 1000000,
                "status": "running",
            }
            mcp_server._persist_inflight()

            assert state_file.exists()
            data = json.loads(state_file.read_text())
            assert "target-1.example.com" in data

    def test_corr_to_target_mapping(self):
        mcp_server._corr_to_target["corr-111"] = "target-1.example.com"
        mcp_server._corr_to_target["corr-222"] = "target-2.example.com"

        assert mcp_server._corr_to_target["corr-111"] == "target-1.example.com"
        assert mcp_server._corr_to_target["corr-222"] == "target-2.example.com"
