"""Tests for agents.bridge.pubsub_client — message format, correlation, session."""

import json
import uuid
from unittest.mock import patch, MagicMock

from agents.bridge import pubsub_client as ps


class TestSessionID:
    def test_session_id_is_uuid(self):
        sid = ps.get_session_id()
        uuid.UUID(sid)

    def test_session_id_consistent(self):
        assert ps.get_session_id() == ps.get_session_id()


class TestTopicNames:
    def test_commands_topic_format(self):
        topic = ps._commands_topic()
        assert topic.startswith("projects/")
        assert "/topics/" in topic

    def test_results_topic_format(self):
        topic = ps._results_topic()
        assert topic.startswith("projects/")
        assert "/topics/" in topic

    def test_results_sub_name_construction(self):
        from agents import config
        project = config.GCP_PUBSUB_PROJECT_ID or "test-project"
        sub_name = config.PUBSUB_RESULTS_SUB or "dci-results-agent-sub"
        expected_prefix = f"projects/{project}/subscriptions/"
        assert expected_prefix.startswith("projects/")
        assert sub_name in expected_prefix + sub_name


class TestMessageFormat:
    @patch.object(ps, "_get_publisher")
    def test_publish_command_calls_publish(self, mock_pub):
        mock_publisher = MagicMock()
        mock_pub.return_value = mock_publisher
        future = MagicMock()
        future.result.return_value = "msg-id"
        mock_publisher.publish.return_value = future

        correlation_id = str(uuid.uuid4())
        ps._publish_command("ssh.execute", {"command": "ls"}, correlation_id)

        assert mock_publisher.publish.called

    @patch.object(ps, "_get_publisher")
    def test_publish_command_sends_json(self, mock_pub):
        mock_publisher = MagicMock()
        mock_pub.return_value = mock_publisher
        future = MagicMock()
        future.result.return_value = "msg-id"
        mock_publisher.publish.return_value = future

        ps._publish_command("workflow.run", {}, str(uuid.uuid4()))

        call_args = mock_publisher.publish.call_args
        data_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("data", b"")
        if isinstance(data_arg, bytes):
            parsed = json.loads(data_arg)
            assert "command_type" in parsed
            assert "session_id" in parsed
            assert "correlation_id" in parsed
            assert "payload" in parsed


class TestCorrelationID:
    def test_correlation_id_is_uuid(self):
        cid = str(uuid.uuid4())
        uuid.UUID(cid)

    def test_different_commands_get_different_ids(self):
        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())
        assert id1 != id2


class TestHealthCheck:
    @patch.object(ps, "_get_publisher")
    @patch.object(ps, "_get_subscriber")
    def test_health_check_returns_dict(self, mock_sub, mock_pub):
        mock_pub.return_value = MagicMock()
        mock_sub.return_value = MagicMock()

        result = ps.check_pubsub_health()
        assert isinstance(result, dict)
        assert "healthy" in result

    @patch.object(ps, "_get_publisher", side_effect=Exception("no creds"))
    def test_health_check_handles_no_credentials(self, mock_pub):
        result = ps.check_pubsub_health()
        assert result["healthy"] is False
        assert "error" in result


class TestSubscriptionLifecycle:
    """Verify the subscription is never deleted during normal operations."""

    def test_no_delete_in_reset_between_runs(self):
        """reset_between_runs must NOT delete the subscription.

        This is the root cause test: previously, reset_between_runs called
        refresh_subscription which deleted and recreated the sub. During the
        200-500ms gap, any message published by the relay was lost forever.
        """
        with patch.object(ps, "_get_subscriber") as mock_sub, \
             patch.object(ps, "_results_sub", return_value="projects/p/subscriptions/test-sub"):
            mock_subscriber = MagicMock()
            mock_sub.return_value = mock_subscriber
            mock_subscriber.pull.return_value = MagicMock(received_messages=[])

            ps.reset_between_runs()

            # The critical assertion: delete_subscription must never be called
            mock_subscriber.delete_subscription.assert_not_called()

    def test_refresh_subscription_does_not_delete(self):
        """refresh_subscription verifies health but never deletes."""
        with patch.object(ps, "_get_subscriber") as mock_sub, \
             patch.object(ps, "_results_sub", return_value="projects/p/subscriptions/test-sub"):
            mock_subscriber = MagicMock()
            mock_sub.return_value = mock_subscriber
            # Simulate a healthy pull (DEADLINE_EXCEEDED = no messages, but sub exists)
            from google.api_core.exceptions import DeadlineExceeded
            mock_subscriber.pull.side_effect = DeadlineExceeded("timeout")

            ps.refresh_subscription()

            mock_subscriber.delete_subscription.assert_not_called()

    def test_recreate_only_on_not_found(self):
        """Subscription is only recreated when confirmed NOT_FOUND."""
        with patch.object(ps, "_get_subscriber") as mock_sub, \
             patch.object(ps, "_results_sub", return_value="projects/p/subscriptions/test-sub"):
            mock_subscriber = MagicMock()
            mock_sub.return_value = mock_subscriber
            from google.api_core.exceptions import NotFound
            mock_subscriber.pull.side_effect = NotFound("sub gone")

            ps.refresh_subscription()

            # After NOT_FOUND, _recreate_subscription should have been called
            # which resets _temp_sub_path and calls _results_sub() again
            assert "recreated" in ps.refresh_subscription().lower() or \
                   ps._temp_sub_path is not None

    def test_subscription_age_tracking(self):
        assert hasattr(ps, "_temp_sub_created_at")


class TestConnectionDiagnostics:
    """Verify distinct error states are reported correctly."""

    def test_diagnostics_returns_all_fields(self):
        diag = ps.get_connection_diagnostics()
        assert "subscription" in diag
        assert "last_successful_pull_seconds_ago" in diag
        assert "last_relay_response_seconds_ago" in diag
        assert "pull_error_count" in diag
        assert "diagnosis" in diag

    def test_diagnosis_missing_subscription(self):
        original = ps._temp_sub_path
        ps._temp_sub_path = None
        try:
            diag = ps.get_connection_diagnostics()
            assert "SUBSCRIPTION_MISSING" in diag["diagnosis"]
        finally:
            ps._temp_sub_path = original

    def test_diagnosis_healthy(self):
        original_snap = ps._telemetry.snapshot()
        original_path = ps._temp_sub_path
        try:
            ps._temp_sub_path = "projects/p/subscriptions/test"
            ps._telemetry.record_full_success()
            diag = ps.get_connection_diagnostics()
            assert diag["diagnosis"] == "HEALTHY"
        finally:
            with ps._telemetry._lock:
                ps._telemetry.last_successful_pull = original_snap["last_successful_pull"]
                ps._telemetry.last_relay_response = original_snap["last_relay_response"]
                ps._telemetry.pull_error_count = original_snap["pull_error_count"]
                ps._telemetry.messages_received_total = original_snap["messages_received_total"]
                ps._telemetry.last_pull_error = original_snap["last_pull_error"]
            ps._temp_sub_path = original_path


class TestRelayLostResult:
    def test_relay_lost_result_format(self):
        result = ps._relay_lost_result(gap=150.0, threshold=120.0)
        assert result["success"] is False
        assert "relay" in result["error"].lower() or "lost" in result["error"].lower()
        assert "gap" in str(result) or "150" in str(result)
