"""Tests for relay/daemon.py — HeartbeatPublisher, _process_message, _process_message_then_ack."""

import json
import time
from unittest.mock import MagicMock, patch

from relay.daemon import HeartbeatPublisher, _process_message, _process_message_then_ack


# ---------------------------------------------------------------------------
# HeartbeatPublisher
# ---------------------------------------------------------------------------

class TestHeartbeatPublisherInit:
    """Verify HeartbeatPublisher starts its background thread and stores attributes."""

    def test_thread_starts_on_init(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="projects/p/topics/t",
            correlation_id="corr-1234-5678",
            command_type="workflow.run",
            session_id="sess-1234",
            interval=3600,  # long interval so it doesn't fire during test
        )
        try:
            assert hb._thread.is_alive()
            assert hb._thread.daemon is True
            assert "heartbeat-corr-123" in hb._thread.name
        finally:
            hb.stop()

    def test_initial_state(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="projects/p/topics/t",
            correlation_id="abc",
            command_type="workflow.run",
            session_id="sess",
            interval=3600,
        )
        try:
            assert hb._seq == 0
            assert hb._last_line == ""
            assert hb._phase == ""
            assert hb._failure_detected is False
        finally:
            hb.stop()


class TestHeartbeatPublisherUpdate:
    """Verify update() stores line and phase without blocking."""

    def test_update_stores_line_and_phase(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        try:
            hb.update(line="TASK [install package]", phase="task:install package")
            assert hb._last_line == "TASK [install package]"
            assert hb._phase == "task:install package"
        finally:
            hb.stop()

    def test_update_truncates_long_lines(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        try:
            long_line = "x" * 500
            hb.update(line=long_line)
            assert len(hb._last_line) == 200
        finally:
            hb.stop()

    def test_update_empty_line_does_not_overwrite(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        try:
            hb.update(line="first line", phase="phase1")
            hb.update(line="", phase="")
            assert hb._last_line == "first line"
            assert hb._phase == "phase1"
        finally:
            hb.stop()

    def test_update_with_phase_only(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        try:
            hb.update(phase="recap")
            assert hb._phase == "recap"
            assert hb._last_line == ""
        finally:
            hb.stop()


class TestHeartbeatPublisherSendNow:
    """Verify send_now() triggers immediate heartbeat with failure_detected."""

    def test_send_now_sets_failure_detected(self):
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        try:
            hb.send_now()
            # Wait for the heartbeat thread to pick up the wake event
            time.sleep(0.5)

            # Verify publish was called
            assert publisher.publish.called
            # Check the published message has failure_detected
            call_args = publisher.publish.call_args
            data = json.loads(call_args[0][1])
            assert data["message_type"] == "heartbeat"
            assert data["heartbeat"]["failure_detected"] is True
        finally:
            hb.stop()

    def test_failure_detected_cleared_after_publish(self):
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        try:
            hb.send_now()
            time.sleep(0.5)
            # After publish, _failure_detected should be cleared
            assert hb._failure_detected is False
        finally:
            hb.stop()


class TestHeartbeatPublisherStop:
    """Verify stop() terminates the thread cleanly."""

    def test_stop_terminates_thread(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        assert hb._thread.is_alive()
        hb.stop()
        hb._thread.join(timeout=2)
        assert not hb._thread.is_alive()

    def test_stop_sets_stopped_event(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        hb.stop()
        assert hb._stopped.is_set()

    def test_stop_is_idempotent(self):
        publisher = MagicMock()
        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=3600,
        )
        hb.stop()
        hb.stop()  # second stop should not raise
        assert hb._stopped.is_set()


class TestHeartbeatPublisherInterval:
    """Verify the heartbeat fires at the configured interval."""

    def test_heartbeat_fires_on_interval(self):
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="projects/p/topics/results",
            correlation_id="corr-id",
            command_type="workflow.run",
            session_id="sess-id",
            interval=0.2,  # 200ms interval for fast test
        )
        try:
            time.sleep(0.5)  # Wait for 2-3 heartbeats
            assert publisher.publish.call_count >= 1
        finally:
            hb.stop()

    def test_heartbeat_message_format(self):
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="projects/p/topics/results",
            correlation_id="corr-id-1234",
            command_type="workflow.run",
            session_id="sess-id-5678",
            interval=0.1,
        )
        try:
            hb.update(line="TASK [install sap]", phase="task:install sap")
            time.sleep(0.3)

            assert publisher.publish.called
            call_args = publisher.publish.call_args
            topic_arg = call_args[0][0]
            data_arg = call_args[0][1]

            assert topic_arg == "projects/p/topics/results"
            msg = json.loads(data_arg)
            assert msg["correlation_id"] == "corr-id-1234"
            assert msg["command_type"] == "workflow.run"
            assert msg["session_id"] == "sess-id-5678"
            assert msg["message_type"] == "heartbeat"
            assert "timestamp" in msg
            assert "heartbeat" in msg
            hb_data = msg["heartbeat"]
            assert "elapsed_seconds" in hb_data
            assert "seq" in hb_data
            assert hb_data["seq"] >= 1
            assert hb_data["phase"] == "task:install sap"
            assert "install sap" in hb_data["last_output_line"]
        finally:
            hb.stop()

    def test_seq_increments(self):
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=0.1,
        )
        try:
            time.sleep(0.35)
            # Should have at least 2 heartbeats
            assert publisher.publish.call_count >= 2
            # Check that seq increases
            seqs = []
            for c in publisher.publish.call_args_list:
                msg = json.loads(c[0][1])
                seqs.append(msg["heartbeat"]["seq"])
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == len(seqs)  # all unique
        finally:
            hb.stop()


class TestHeartbeatPublisherPublishFailure:
    """Verify heartbeat continues even if publish fails."""

    def test_continues_on_publish_error(self):
        publisher = MagicMock()
        future = MagicMock()
        future.result.side_effect = Exception("Pub/Sub unavailable")
        publisher.publish.return_value = future

        hb = HeartbeatPublisher(
            publisher=publisher,
            results_topic="t",
            correlation_id="c",
            command_type="workflow.run",
            session_id="s",
            interval=0.1,
        )
        try:
            time.sleep(0.35)
            # Should have attempted multiple publishes despite errors
            assert publisher.publish.call_count >= 2
            # Thread should still be alive
            assert hb._thread.is_alive()
        finally:
            hb.stop()


# ---------------------------------------------------------------------------
# _process_message_then_ack
# ---------------------------------------------------------------------------

class TestProcessMessageThenAck:
    """Verify ACK happens after processing, not before."""

    @patch("relay.daemon._process_message")
    @patch("relay.daemon._audit_log")
    def test_ack_called_after_processing(self, mock_audit, mock_process):
        """ACK must happen after _process_message completes."""
        call_order = []
        mock_process.side_effect = lambda *args, **kwargs: call_order.append("process")

        subscriber = MagicMock()
        subscriber.acknowledge.side_effect = lambda **kwargs: call_order.append("ack")

        ssh = MagicMock()
        publisher = MagicMock()
        message_data = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "ssh.execute",
            "payload": {"command": "ls"},
        }).encode()

        _process_message_then_ack(
            message_data=message_data,
            ssh=ssh,
            publisher=publisher,
            results_topic="t",
            subscriber=subscriber,
            subscription="sub",
            ack_id="ack-1",
        )

        assert call_order == ["process", "ack"]

    @patch("relay.daemon._audit_log")
    def test_ack_called_even_on_handler_error(self, mock_audit):
        """ACK must happen even if the handler raises an exception.

        We register a handler that raises and verify ACK still fires
        after _process_message handles the exception internally.
        """
        def crashing_handler(ssh, payload):
            raise RuntimeError("handler crashed")

        subscriber = MagicMock()
        ssh = MagicMock()
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        message_data = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "crash.handler",
            "payload": {},
            "session_id": "sess-1",
        }).encode()

        with patch("relay.daemon.HANDLERS", {"crash.handler": crashing_handler}):
            _process_message_then_ack(
                message_data=message_data,
                ssh=ssh,
                publisher=publisher,
                results_topic="t",
                subscriber=subscriber,
                subscription="sub",
                ack_id="ack-1",
            )

        subscriber.acknowledge.assert_called_once_with(
            subscription="sub", ack_ids=["ack-1"]
        )

    @patch("relay.daemon._process_message")
    @patch("relay.daemon._audit_log")
    def test_ack_failure_does_not_raise(self, mock_audit, mock_process):
        """If ACK fails, the error is logged but not raised."""
        subscriber = MagicMock()
        subscriber.acknowledge.side_effect = Exception("ACK failed")

        ssh = MagicMock()
        publisher = MagicMock()
        message_data = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "ssh.execute",
            "payload": {},
        }).encode()

        # Should not raise
        _process_message_then_ack(
            message_data=message_data,
            ssh=ssh,
            publisher=publisher,
            results_topic="t",
            subscriber=subscriber,
            subscription="sub",
            ack_id="ack-1",
        )


# ---------------------------------------------------------------------------
# _process_message
# ---------------------------------------------------------------------------

class TestProcessMessage:
    """Test _process_message dispatching and result publishing."""

    @patch("relay.daemon._audit_log")
    def test_malformed_message_is_discarded(self, mock_audit):
        """Non-JSON messages should be silently discarded."""
        ssh = MagicMock()
        publisher = MagicMock()

        _process_message(b"not json {{", ssh, publisher, "t")

        # No publish should happen — the message is discarded
        publisher.publish.assert_not_called()

    @patch("relay.daemon._audit_log")
    @patch("relay.daemon.HANDLERS", {"test.cmd": lambda ssh, payload: {"success": True, "data": "ok"}})
    def test_dispatches_to_correct_handler(self, mock_audit):
        """Messages should be dispatched to the handler registered for their command_type."""
        ssh = MagicMock()
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        msg = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "test.cmd",
            "payload": {"key": "value"},
            "session_id": "sess-1",
        }).encode()

        _process_message(msg, ssh, publisher, "projects/p/topics/results")

        assert publisher.publish.called
        call_args = publisher.publish.call_args
        topic = call_args[0][0]
        data = json.loads(call_args[0][1])

        assert topic == "projects/p/topics/results"
        assert data["correlation_id"] == "corr-1"
        assert data["command_type"] == "test.cmd"
        assert data["message_type"] == "final"
        assert data["result"]["success"] is True

    @patch("relay.daemon._audit_log")
    def test_unknown_command_type_returns_error(self, mock_audit):
        """Unknown command types should return an error result."""
        ssh = MagicMock()
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        msg = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "nonexistent.cmd",
            "payload": {},
            "session_id": "sess-1",
        }).encode()

        _process_message(msg, ssh, publisher, "t")

        call_args = publisher.publish.call_args
        data = json.loads(call_args[0][1])
        assert "error" in data["result"]
        assert "Unknown command type" in data["result"]["error"]

    @patch("relay.daemon._audit_log")
    @patch("relay.daemon.HANDLERS", {"crash.cmd": lambda ssh, payload: (_ for _ in ()).throw(ValueError("boom"))})
    def test_handler_exception_returns_error_result(self, mock_audit):
        """If a handler raises, the result should contain the error."""
        ssh = MagicMock()
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        msg = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "crash.cmd",
            "payload": {},
            "session_id": "sess-1",
        }).encode()

        _process_message(msg, ssh, publisher, "t")

        call_args = publisher.publish.call_args
        data = json.loads(call_args[0][1])
        assert "error" in data["result"]
        assert "Handler exception" in data["result"]["error"]

    @patch("relay.daemon._audit_log")
    @patch("relay.daemon.HANDLERS", {"hb.cmd": lambda ssh, payload: {"success": True}})
    @patch("relay.daemon.LONG_RUNNING_COMMANDS", {"hb.cmd"})
    def test_heartbeat_publisher_created_for_long_running(self, mock_audit):
        """Long-running commands with _heartbeat_capable should get a HeartbeatPublisher."""
        ssh = MagicMock()
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        received_payload = {}

        def capture_handler(ssh, payload):
            received_payload.update(payload)
            return {"success": True}

        with patch("relay.daemon.HANDLERS", {"hb.cmd": capture_handler}):
            msg = json.dumps({
                "correlation_id": "corr-1",
                "command_type": "hb.cmd",
                "payload": {},
                "session_id": "sess-1",
                "_heartbeat_capable": True,
            }).encode()

            _process_message(msg, ssh, publisher, "t")

        assert "_heartbeat_publisher" in received_payload
        assert isinstance(received_payload["_heartbeat_publisher"], HeartbeatPublisher)

    @patch("relay.daemon._audit_log")
    @patch("relay.daemon.HANDLERS", {"ack.cmd": lambda ssh, payload: {"success": True}})
    def test_ack_message_published_for_heartbeat_capable(self, mock_audit):
        """When _heartbeat_capable is True, an ACK message should be published first."""
        ssh = MagicMock()
        publisher = MagicMock()
        future = MagicMock()
        future.result.return_value = None
        publisher.publish.return_value = future

        msg = json.dumps({
            "correlation_id": "corr-1",
            "command_type": "ack.cmd",
            "payload": {},
            "session_id": "sess-1",
            "_heartbeat_capable": True,
        }).encode()

        _process_message(msg, ssh, publisher, "t")

        # Should have published at least 2 messages: ACK + final result
        assert publisher.publish.call_count >= 2
        # First publish should be the ACK
        first_data = json.loads(publisher.publish.call_args_list[0][0][1])
        assert first_data["message_type"] == "ack"
        assert first_data["correlation_id"] == "corr-1"

    @patch("relay.daemon._audit_log")
    def test_publish_retries_on_failure(self, mock_audit):
        """Result publishing should retry up to 3 times on failure."""
        ssh = MagicMock()
        publisher = MagicMock()

        # Fail twice, succeed on third
        fail_future = MagicMock()
        fail_future.result.side_effect = Exception("network error")
        ok_future = MagicMock()
        ok_future.result.return_value = None
        publisher.publish.side_effect = [ok_future]  # We'll use a handler that returns quickly

        with patch("relay.daemon.HANDLERS", {"retry.cmd": lambda ssh, payload: {"success": True}}):
            publisher.publish.side_effect = [fail_future, fail_future, ok_future]

            msg = json.dumps({
                "correlation_id": "corr-1",
                "command_type": "retry.cmd",
                "payload": {},
                "session_id": "sess-1",
            }).encode()

            with patch("relay.daemon.time") as mock_time:
                mock_time.time.return_value = 1000.0
                mock_time.sleep = MagicMock()
                _process_message(msg, ssh, publisher, "t")

            assert publisher.publish.call_count == 3
