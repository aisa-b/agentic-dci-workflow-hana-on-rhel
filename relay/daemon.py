"""
Relay daemon -- Pub/Sub subscriber that dispatches commands to handlers.

Runs on the relay machine. Subscribes to the dci-commands topic,
executes operations on the jumpbox via SSH, and publishes results to
the dci-results topic.

Threading: workflow.run runs in a separate thread so SSH diagnostics
can proceed concurrently.

Usage:
    python -m relay.daemon
"""

import json
import logging
import os
import signal
import sys
import time
import datetime
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from google.cloud import pubsub_v1

from . import config
from .ssh_manager import SSHManager
from .handlers import (
    HANDLERS, _active_workflows, _active_workflows_lock,
    _completed_workflows, _completed_workflows_lock,
)

logger = logging.getLogger(__name__)

_running = True
_relay_git_sha: str = "unknown"
_relay_start_time: float = 0.0

LONG_RUNNING_COMMANDS = {"workflow.run"}


def _get_git_sha() -> str:
    """Get the current git SHA of the relay code."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _handle_signal(signum, frame):
    global _running
    logger.info("Received signal %d, shutting down gracefully...", signum)
    _running = False


def _watchdog(ssh: SSHManager, interval: int = 60):
    """Background thread that auto-kills stuck workflows and checks connectivity."""
    while _running:
        time.sleep(interval)
        if not _running:
            break
        try:
            with _active_workflows_lock:
                stuck = [
                    (host, info) for host, info in _active_workflows.items()
                    if time.time() - info["start_time"] > config.WORKFLOW_TIMEOUT
                ]
            for host, info in stuck:
                elapsed = int(time.time() - info["start_time"])
                logger.warning(
                    "Watchdog: workflow for %s exceeded timeout (%ds > %ds), killing",
                    host, elapsed, config.WORKFLOW_TIMEOUT,
                )
                import shlex as _shlex
                settings_basename = os.path.basename(info["settings_file"])
                kill_cmd = f"sudo pkill -f {_shlex.quote(settings_basename)} || true"
                ssh.exec_on_jumpbox(kill_cmd, timeout=15)

            now = time.time()
            with _completed_workflows_lock:
                expired = [k for k, v in _completed_workflows.items() if now > v["expires_at"]]
                for k in expired:
                    del _completed_workflows[k]
                if expired:
                    logger.info("Watchdog: evicted %d expired completion(s)", len(expired))

            with _active_workflows_lock:
                has_active = bool(_active_workflows)
            if not has_active:
                test = ssh.exec_on_jumpbox("echo watchdog_ok", timeout=10)
                if not test["success"]:
                    logger.warning("Watchdog: control connection unhealthy, will reconnect on next use")
        except Exception as e:
            logger.error("Watchdog error: %s", e)


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


def _audit_log(entry: dict):
    """Append a JSON entry to the local audit log."""
    log_path = Path(config.AUDIT_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = datetime.datetime.now().isoformat()
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


class HeartbeatPublisher:
    """Publishes periodic heartbeat messages on the results topic.

    The handler calls update() to provide the latest output line and phase.
    A background timer thread publishes heartbeats at the configured interval.
    update() is just an attribute assignment — no blocking, no lock.
    """

    def __init__(self, publisher, results_topic, correlation_id, command_type,
                 session_id, interval=120):
        self._publisher = publisher
        self._topic = results_topic
        self._correlation_id = correlation_id
        self._command_type = command_type
        self._session_id = session_id
        self._interval = interval
        self._start_time = time.time()
        self._last_line = ""
        self._phase = ""
        self._seq = 0
        self._failure_detected = False
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"heartbeat-{correlation_id[:8]}",
        )
        self._thread.start()

    def update(self, line: str = "", phase: str = ""):
        if line:
            self._last_line = line[-200:]
        if phase:
            self._phase = phase

    def send_now(self):
        """Interrupt the sleep and send a heartbeat immediately (on failure)."""
        self._failure_detected = True
        self._wake.set()

    def stop(self):
        self._stopped.set()
        self._wake.set()

    def _run(self):
        while not self._stopped.is_set():
            self._wake.wait(timeout=self._interval)
            self._wake.clear()
            if self._stopped.is_set():
                break
            self._seq += 1
            heartbeat_data = {
                "elapsed_seconds": round(time.time() - self._start_time),
                "last_output_line": self._last_line,
                "phase": self._phase,
                "seq": self._seq,
            }
            if self._failure_detected:
                heartbeat_data["failure_detected"] = True
                self._failure_detected = False
            msg = {
                "correlation_id": self._correlation_id,
                "command_type": self._command_type,
                "session_id": self._session_id,
                "message_type": "heartbeat",
                "timestamp": time.time(),
                "heartbeat": heartbeat_data,
            }
            try:
                data = json.dumps(msg).encode("utf-8")
                future = self._publisher.publish(self._topic, data)
                future.result(timeout=10)
                logger.debug(
                    "Heartbeat #%d for %s (elapsed=%ds, phase=%s)",
                    self._seq, self._correlation_id[:8],
                    round(time.time() - self._start_time), self._phase,
                )
            except Exception as e:
                logger.warning("Heartbeat publish failed: %s", e)


def _process_message_then_ack(
    message_data: bytes,
    ssh: SSHManager,
    publisher,
    results_topic: str,
    subscriber,
    subscription: str,
    ack_id: str,
) -> None:
    """Process a short command and ACK only after the result is published."""
    try:
        _process_message(message_data, ssh, publisher, results_topic)
    finally:
        try:
            subscriber.acknowledge(subscription=subscription, ack_ids=[ack_id])
        except Exception as e:
            logger.warning("Deferred ACK failed (message may redeliver): %s", e)


def _process_message(
    message_data: bytes,
    ssh: SSHManager,
    publisher,
    results_topic: str,
) -> None:
    """Parse a command message, dispatch to the handler, and publish the result."""
    try:
        command = json.loads(message_data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Malformed message, discarding: %s", e)
        _audit_log({"event": "malformed_message", "error": str(e)})
        return

    correlation_id = command.get("correlation_id", "unknown")
    command_type = command.get("command_type", "")
    payload = command.get("payload", {})
    session_id = command.get("session_id", "")
    heartbeat_capable = command.get("_heartbeat_capable", False)

    logger.info(
        "Received command: %s (corr: %s, session: %s)",
        command_type, correlation_id[:8] if isinstance(correlation_id, str) else "?",
        session_id[:8] if isinstance(session_id, str) else "?",
    )

    _audit_log({
        "event": "command_received",
        "correlation_id": correlation_id,
        "command_type": command_type,
        "session_id": session_id,
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
    })

    if heartbeat_capable:
        ack_msg = {
            "correlation_id": correlation_id,
            "command_type": command_type,
            "session_id": session_id,
            "message_type": "ack",
            "timestamp": time.time(),
        }
        try:
            ack_data = json.dumps(ack_msg).encode("utf-8")
            future = publisher.publish(results_topic, ack_data)
            future.result(timeout=10)
            logger.info("Published ACK for %s", correlation_id[:8])
        except Exception as e:
            logger.warning("Failed to publish ACK for %s: %s", correlation_id[:8], e)

    hb_publisher = None
    handler = HANDLERS.get(command_type)
    if handler is None:
        result = {"error": f"Unknown command type: {command_type}"}
        logger.warning("Unknown command type: %s", command_type)
    else:
        try:
            if heartbeat_capable and command_type in LONG_RUNNING_COMMANDS:
                hb_publisher = HeartbeatPublisher(
                    publisher=publisher,
                    results_topic=results_topic,
                    correlation_id=correlation_id,
                    command_type=command_type,
                    session_id=session_id,
                    interval=120,
                )
                payload["_heartbeat_publisher"] = hb_publisher

            start = time.time()
            payload["_correlation_id"] = correlation_id
            result = handler(ssh, payload)
            elapsed = time.time() - start
            logger.info(
                "Command %s completed (%.1fs, success=%s)",
                command_type, elapsed, result.get("success", "N/A"),
            )
        except Exception as e:
            logger.exception("Handler %s raised an exception", command_type)
            result = {"error": f"Handler exception: {str(e)[:500]}"}
        finally:
            if hb_publisher:
                hb_publisher.stop()

    response = {
        "correlation_id": correlation_id,
        "command_type": command_type,
        "session_id": session_id,
        "message_type": "final",
        "result": result,
    }

    _audit_log({
        "event": "result_published",
        "correlation_id": correlation_id,
        "command_type": command_type,
        "success": result.get("success", None),
        "error": result.get("error", None),
    })

    response_data = json.dumps(response).encode("utf-8")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            future = publisher.publish(results_topic, response_data)
            future.result(timeout=30)
            logger.info("Published result for %s", correlation_id[:8] if isinstance(correlation_id, str) else "?")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    "Publish failed for %s (attempt %d/%d, retrying in %ds): %s",
                    correlation_id[:8], attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Publish FAILED for %s after %d attempts — result lost: %s",
                    correlation_id[:8], max_retries, e,
                )


def main():
    _setup_logging()

    problems = config.validate()
    if problems:
        print("Configuration errors:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nSee SETUP_GUIDE.md for required variables.", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    commands_sub = f"projects/{config.GCP_PUBSUB_PROJECT_ID}/subscriptions/{config.PUBSUB_COMMANDS_SUB}"
    results_topic = f"projects/{config.GCP_PUBSUB_PROJECT_ID}/topics/{config.PUBSUB_RESULTS_TOPIC}"

    global _relay_git_sha, _relay_start_time
    _relay_git_sha = _get_git_sha()
    _relay_start_time = time.time()

    print("=" * 50)
    print("DCI Relay Daemon")
    print(f"  Git SHA:      {_relay_git_sha}")
    print(f"  Jumpbox:      {config.JUMPBOX_HOST} (user: {config.JUMPBOX_USER})")
    print(f"  Jumpbox repo: {config.REPO_ROOT}")
    print(f"  Hooks dir:    {config.HOOKS_DIR}")
    print(f"  Listening on: {config.PUBSUB_COMMANDS_SUB}")
    print(f"  Publishing to: {config.PUBSUB_RESULTS_TOPIC}")
    print(f"  Audit log:    {config.AUDIT_LOG}")
    print("  Threading:    workflow in background, SSH in foreground")
    print("=" * 50)

    ssh = SSHManager()
    ssh.start_keepalive(interval=60)
    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient()

    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="relay")

    logger.info("Testing jumpbox connection...")
    test = ssh.exec_on_jumpbox("hostname", timeout=10)
    if test["success"]:
        logger.info("Jumpbox reachable: %s", test["stdout"].strip())
    else:
        logger.error("Cannot reach jumpbox: %s", test["stderr"])
        sys.exit(1)

    # [AGENT-ADDED] Verify hooks repo is cloneable at startup, not at workflow time
    from .handlers import _is_git_url
    if _is_git_url(config.HOOKS_DIR):
        logger.info("Verifying hooks repo access: %s", config.HOOKS_DIR)
        hooks_test = ssh.exec_on_jumpbox(
            f'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=no -o BatchMode=yes" '
            f'git ls-remote {config.HOOKS_DIR} HEAD 2>&1 | head -1',
            timeout=20,
        )
        if hooks_test["success"] and hooks_test["stdout"].strip():
            logger.info("Hooks repo accessible: %s", hooks_test["stdout"].strip()[:80])
        else:
            logger.error("HOOKS REPO NOT ACCESSIBLE: %s — workflows will fail at clone step", hooks_test.get("stderr", hooks_test.get("stdout", ""))[:200])
    else:
        logger.info("Hooks dir is local path: %s", config.HOOKS_DIR)

    watchdog_thread = threading.Thread(
        target=_watchdog, args=(ssh,), daemon=True, name="relay-watchdog"
    )
    watchdog_thread.start()
    logger.info("Watchdog started (interval=60s, timeout=%ds)", config.WORKFLOW_TIMEOUT)

    # [AGENT-ADDED] Drain any messages left in-flight from a previous instance
    # (e.g., after relay.update restart). Without this, the new subscriber may
    # get nothing for up to 60s while Pub/Sub holds messages for the dead consumer.
    try:
        drain_resp = subscriber.pull(subscription=commands_sub, max_messages=50, timeout=3)
        if drain_resp.received_messages:
            ack_ids = [m.ack_id for m in drain_resp.received_messages]
            subscriber.acknowledge(subscription=commands_sub, ack_ids=ack_ids)
            logger.info("Startup: drained %d stale command(s) from previous instance", len(ack_ids))
    except Exception:
        pass

    logger.info("Relay is running. Waiting for commands... (PID=%d)", os.getpid())
    sys.stdout.flush()

    consecutive_errors = 0

    try:
        while _running:
            try:
                response = subscriber.pull(
                    subscription=commands_sub,
                    max_messages=5,
                    timeout=30,
                )
                consecutive_errors = 0
            except Exception as e:
                if "504" in str(e) or "DEADLINE_EXCEEDED" in str(e):
                    consecutive_errors = 0
                    continue
                consecutive_errors += 1
                logger.error("Pub/Sub pull error (#%d): %s", consecutive_errors, e)
                if consecutive_errors >= 3:
                    logger.warning("Recreating Pub/Sub subscriber after %d consecutive errors", consecutive_errors)
                    try:
                        subscriber.close()
                    except Exception:
                        pass
                    subscriber = pubsub_v1.SubscriberClient()
                    consecutive_errors = 0
                time.sleep(min(5 * consecutive_errors, 30))
                continue

            if not response.received_messages:
                continue

            for received_message in response.received_messages:
                try:
                    data = received_message.message.data

                    try:
                        cmd = json.loads(data.decode("utf-8"))
                        cmd_type = cmd.get("command_type", "")
                    except Exception:
                        cmd_type = ""

                    ack_id = received_message.ack_id
                    if cmd_type in LONG_RUNNING_COMMANDS:
                        logger.info("Dispatching %s to background thread (ACK now)", cmd_type)
                        subscriber.acknowledge(
                            subscription=commands_sub,
                            ack_ids=[ack_id],
                        )
                        executor.submit(
                            _process_message, data, ssh, publisher, results_topic
                        )
                    else:
                        logger.info("Dispatching %s (ACK after completion)", cmd_type)
                        executor.submit(
                            _process_message_then_ack,
                            data, ssh, publisher, results_topic,
                            subscriber, commands_sub, ack_id,
                        )
                except Exception as e:
                    logger.error("Failed to process message: %s", e, exc_info=True)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error("FATAL: Main loop crashed: %s", e, exc_info=True)
        print(f"FATAL: Main loop crashed: {e}", file=sys.stderr, flush=True)
    finally:
        executor.shutdown(wait=True)
        ssh.close()
        subscriber.close()
        logger.info("Relay daemon stopped")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(f"DAEMON EXIT: SystemExit code={e.code}", file=sys.stderr, flush=True)
        raise
    except Exception as e:
        import traceback
        print(f"DAEMON CRASH: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        sys.exit(1)
    finally:
        print("DAEMON PROCESS ENDING", file=sys.stderr, flush=True)
        sys.stderr.flush()
