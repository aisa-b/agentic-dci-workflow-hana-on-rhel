"""Integration tests -- require live GCP credentials and running relay.

Skipped automatically when credentials are not available.
Run explicitly: pytest tests/test_integration.py -q

These tests hit real infrastructure:
- Google Cloud Pub/Sub (publishes and subscribes)
- Relay daemon (must be running on relay machine)
- Jumpbox (SSH tunnel via relay)

They do NOT modify any server state -- all operations are read-only.
"""

import os
import pytest

from pathlib import Path


def _has_credentials():
    sa_key = os.environ.get("PUBSUB_SA_KEY_PATH", "") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    if sa_key and Path(sa_key).exists():
        return True
    default_key = Path(__file__).resolve().parent.parent / "infra" / "dci-relay-sa-key.json"
    return default_key.exists()


def _has_project_id():
    return bool(os.environ.get("GCP_PUBSUB_PROJECT_ID", ""))


skip_no_creds = pytest.mark.skipif(
    not _has_credentials() or not _has_project_id(),
    reason="GCP credentials or project ID not available (set .env and PUBSUB_SA_KEY_PATH)",
)


@skip_no_creds
class TestPubSubConnectivity:
    """Test real Pub/Sub connection without touching servers."""

    def test_pubsub_health_check(self):
        from agents.bridge.pubsub_client import check_pubsub_health

        result = check_pubsub_health()
        assert isinstance(result, dict)
        assert "healthy" in result
        assert result["healthy"] is True, f"Pub/Sub unhealthy: {result.get('error')}"

    def test_session_id_persists(self):
        from agents.bridge.pubsub_client import get_session_id

        sid1 = get_session_id()
        sid2 = get_session_id()
        assert sid1 == sid2
        assert len(sid1) > 8


@skip_no_creds
class TestRelayConnectivity:
    """Test real relay communication via Pub/Sub."""

    def test_preflight_check(self):
        from agents.bridge.pubsub_client import send_command
        import asyncio

        async def _run():
            result = await send_command(
                "jumpbox.ping", {}, timeout=30,
            )
            return result

        result = asyncio.run(_run())
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Preflight failed: {result}"

    def test_jumpbox_ping(self):
        from agents.bridge.pubsub_client import send_command
        import asyncio

        async def _run():
            result = await send_command(
                "jumpbox.ping", {}, timeout=30,
            )
            return result

        result = asyncio.run(_run())
        assert result.get("success") is True
        stdout = result.get("stdout", "")
        assert "RELAY_PING_OK" in stdout, f"Ping response missing RELAY_PING_OK: {stdout[:200]}"

    def test_workflow_list(self):
        from agents.bridge.pubsub_client import send_command
        import asyncio

        async def _run():
            result = await send_command(
                "workflow.list", {}, timeout=30,
            )
            return result

        result = asyncio.run(_run())
        assert isinstance(result, dict)
        assert result.get("success") is True, f"workflow.list failed: {result}"
        assert "workflows" in result or "count" in result


_has_mcp = True
try:
    import mcp as _mcp_mod  # noqa: F401
except ImportError:
    _has_mcp = False


@skip_no_creds
@pytest.mark.skipif(not _has_mcp, reason="mcp SDK not installed")
class TestMCPServerIntegration:
    """Test the MCP server can start and serve tools."""

    def test_mcp_server_imports(self):
        from agents.mcp_server import mcp
        assert mcp is not None
        assert mcp.name == "dci-relay"

    def test_mcp_tools_registered(self):
        from agents.mcp_server import mcp
        tools = mcp._tool_manager._tools
        expected_tools = [
            "dci_preflight_check",
            "dci_workflow_run",
            "dci_workflow_status",
            "dci_workflow_stop",
            "dci_workflow_list",
            "dci_ssh_execute",
            "dci_ssh_diagnostics",
            "dci_jumpbox_ping",
            "dci_jumpbox_execute",
            "dci_relay_update",
            "dci_relay_health",
            "dci_server_profile",
            "dci_check_events",
        ]
        registered = list(tools.keys())
        for tool_name in expected_tools:
            assert tool_name in registered, f"MCP tool {tool_name} not registered"


@skip_no_creds
class TestKnowledgeBaseIntegration:
    """Test KB works end-to-end with real file I/O."""

    def test_record_search_cycle(self):
        import tempfile
        from agents.local import knowledge_base as kb

        orig_path = kb._KB_PATH
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            tmp.write(b"[]")
            tmp.close()
            kb._KB_PATH = Path(tmp.name)

            kb.record_fix(
                error_pattern="integration test error",
                diagnosis="integration test diagnosis",
                fix_applied="integration test fix",
                files_changed=["test.yml"],
                success=True,
                target_host="integration-test.example.com",
                rhel_version="RHEL-10.0",
            )

            summary = kb.get_knowledge_summary()
            assert "1 entries" in summary
            assert "integration test error" in summary

            stats = kb.get_category_stats()
            assert isinstance(stats, dict)

        finally:
            kb._KB_PATH = orig_path
            Path(tmp.name).unlink(missing_ok=True)
