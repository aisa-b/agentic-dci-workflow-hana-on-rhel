"""
Pub/Sub bridge client for the agent side (Mac).

Provides send_command() which publishes a command to the dci-commands topic,
then polls the dci-results subscription for the response with a matching
correlation_id.

Includes usage tracking to prevent exceeding the Pub/Sub free tier (10 GiB/month).
"""

import asyncio
import atexit
import functools
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from google.cloud import pubsub_v1
from google.api_core.exceptions import AlreadyExists, NotFound
from google.oauth2 import service_account
from google.protobuf.duration_pb2 import Duration

from .. import config
from . import usage_tracker

logger = logging.getLogger(__name__)

_publisher: pubsub_v1.PublisherClient | None = None
_subscriber: pubsub_v1.SubscriberClient | None = None
_session_id: str = ""
_executor = ThreadPoolExecutor(max_workers=6)

_pending_results: dict[str, dict] = {}
_pending_acks: dict[str, bool] = {}
_pending_lock = threading.Lock()

_temp_sub_name: str | None = None
_temp_sub_path: str | None = None
_temp_sub_created_at: float = 0.0
_sub_creation_lock = threading.Lock()

# Background completion poller
_bg_poller_thread: threading.Thread | None = None
_bg_poller_stop = threading.Event()
_latest_heartbeats: dict[str, dict] = {}
_heartbeat_lock = threading.Lock()
_completion_callbacks: list = []
_heartbeat_callbacks: list = []

class _PubSubTelemetry:
    """Thread-safe telemetry for subscription and relay health diagnostics."""

    def __init__(self):
        self._lock = threading.Lock()
        self.last_successful_pull: float = 0.0
        self.last_relay_response: float = 0.0
        self.pull_error_count: int = 0
        self.messages_received_total: int = 0
        self.last_pull_error: str = ""

    def record_pull_success(self):
        with self._lock:
            self.last_successful_pull = time.time()
            self.pull_error_count = 0

    def record_pull_error(self, error: str):
        with self._lock:
            self.pull_error_count += 1
            self.last_pull_error = error[:300]

    def record_relay_response(self):
        with self._lock:
            self.last_relay_response = time.time()
            self.messages_received_total += 1

    def record_full_success(self):
        with self._lock:
            self.last_successful_pull = time.time()
            self.last_relay_response = time.time()
            self.messages_received_total += 1
            self.pull_error_count = 0

    def reset_errors(self):
        with self._lock:
            self.pull_error_count = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_successful_pull": self.last_successful_pull,
                "last_relay_response": self.last_relay_response,
                "pull_error_count": self.pull_error_count,
                "messages_received_total": self.messages_received_total,
                "last_pull_error": self.last_pull_error,
            }

    def is_stale(self, threshold_seconds: float) -> bool:
        with self._lock:
            return (self.last_successful_pull > 0
                    and time.time() - self.last_successful_pull > threshold_seconds)

    def error_count_exceeds(self, threshold: int) -> bool:
        with self._lock:
            return self.pull_error_count >= threshold


_telemetry = _PubSubTelemetry()


def _get_pubsub_credentials():
    """Load SA key explicitly for Pub/Sub so it doesn't conflict with Vertex AI ADC.

    Uses PUBSUB_SA_KEY_PATH (preferred) or GOOGLE_APPLICATION_CREDENTIALS as fallback.
    The Anthropic SDK must use gcloud application-default credentials for Vertex AI,
    so we load the Pub/Sub SA key explicitly instead of relying on the env var.
    """
    sa_key_path = os.environ.get("PUBSUB_SA_KEY_PATH", "") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if sa_key_path and os.path.exists(sa_key_path):
        return service_account.Credentials.from_service_account_file(
            sa_key_path,
            scopes=["https://www.googleapis.com/auth/pubsub"],
        )
    return None


_client_lock = threading.Lock()


def _get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher
    with _client_lock:
        if _publisher is None:
            creds = _get_pubsub_credentials()
            _publisher = pubsub_v1.PublisherClient(credentials=creds) if creds else pubsub_v1.PublisherClient()
        return _publisher


def _get_subscriber() -> pubsub_v1.SubscriberClient:
    global _subscriber
    with _client_lock:
        if _subscriber is None:
            creds = _get_pubsub_credentials()
            _subscriber = pubsub_v1.SubscriberClient(credentials=creds) if creds else pubsub_v1.SubscriberClient()
        return _subscriber


def _reset_clients() -> None:
    """Close and reset Pub/Sub clients so the next call creates fresh ones.

    Call this after a persistent error (auth failure, transport closed) to
    force reconnection instead of reusing a broken client.
    """
    global _publisher, _subscriber
    with _client_lock:
        if _subscriber:
            try:
                _subscriber.close()
            except Exception:
                pass
            _subscriber = None
        _publisher = None
    logger.info("Pub/Sub clients reset — will reconnect on next use")


def get_session_id() -> str:
    """Return the current session ID, creating one if needed."""
    global _session_id
    if not _session_id:
        _session_id = str(uuid.uuid4())
    return _session_id


def _commands_topic() -> str:
    return f"projects/{config.GCP_PUBSUB_PROJECT_ID}/topics/{config.PUBSUB_COMMANDS_TOPIC}"


def _results_topic() -> str:
    return f"projects/{config.GCP_PUBSUB_PROJECT_ID}/topics/{config.PUBSUB_RESULTS_TOPIC}"


def _results_sub() -> str:
    """Return the per-process temporary subscription path, creating if needed.

    Each MCP server process gets its own subscription on the dci-results topic
    so concurrent sessions don't steal each other's messages. Created once on
    first use and reused for the session lifetime. The 24h TTL handles cleanup
    if the process dies without calling atexit.

    Never deletes the subscription proactively — that causes a gap where
    messages are lost.
    """
    global _temp_sub_name, _temp_sub_path, _temp_sub_created_at

    if _temp_sub_path is not None:
        return _temp_sub_path

    with _sub_creation_lock:
        if _temp_sub_path is not None:
            return _temp_sub_path

        suffix = get_session_id()[:8]
        sub_name = f"{config.PUBSUB_RESULTS_SUB}-{suffix}"
        sub_path = f"projects/{config.GCP_PUBSUB_PROJECT_ID}/subscriptions/{sub_name}"
        topic = _results_topic()

        subscriber = _get_subscriber()
        t0 = time.time()
        try:
            subscriber.create_subscription(
                request={
                    "name": sub_path,
                    "topic": topic,
                    "ack_deadline_seconds": 60,
                    "expiration_policy": {"ttl": Duration(seconds=86400)},
                },
            )
            logger.info("Created subscription %s on topic %s (%.0fms)", sub_name, topic, (time.time() - t0) * 1000)
        except AlreadyExists:
            logger.info("Subscription %s already exists, reusing (%.0fms)", sub_name, (time.time() - t0) * 1000)

        _temp_sub_name = sub_name
        _temp_sub_path = sub_path
        _temp_sub_created_at = time.time()
        atexit.register(_cleanup_temp_subscription)

        try:
            _cleanup_orphaned_subscriptions()
        except Exception as e:
            logger.warning("Orphan cleanup failed (non-fatal): %s", e)

        return _temp_sub_path


def _recreate_subscription():
    """Recreate the subscription only when it's confirmed broken (NOT_FOUND)."""
    global _temp_sub_name, _temp_sub_path, _temp_sub_created_at
    with _sub_creation_lock:
        old = _temp_sub_name
        _temp_sub_name = None
        _temp_sub_path = None
        _temp_sub_created_at = 0.0
        logger.warning("Subscription %s confirmed dead, recreating", old)
    _results_sub()


def _cleanup_temp_subscription() -> None:
    global _temp_sub_name, _temp_sub_path
    if _temp_sub_path is None:
        return
    try:
        subscriber = _get_subscriber()
        subscriber.delete_subscription(subscription=_temp_sub_path)
        logger.info("Deleted temp subscription %s", _temp_sub_name)
    except (NotFound, Exception) as e:
        logger.warning("Could not delete temp subscription %s: %s (expires in 24h)", _temp_sub_name, e)
    _temp_sub_name = None
    _temp_sub_path = None


def refresh_subscription() -> str:
    """Verify the subscription is healthy. Recreate if broken or stale.

    Checks both the subscription existence (test pull) AND the pull health
    metrics (error count, staleness). Recreates on NOT_FOUND or when the
    poller has accumulated too many errors.
    """
    sub_path = _results_sub()

    snap = _telemetry.snapshot()
    if _telemetry.error_count_exceeds(10) or _telemetry.is_stale(300):
        stale_seconds = int(time.time() - snap["last_successful_pull"]) if snap["last_successful_pull"] > 0 else -1
        logger.warning("Subscription stale: %d errors, last pull %ds ago — recreating",
                        snap["pull_error_count"], stale_seconds)
        _reset_clients()
        _recreate_subscription()
        _telemetry.reset_errors()
        return f"Subscription recreated: {_temp_sub_name} (stale: {snap['pull_error_count']} errors, {stale_seconds}s since last pull)"

    try:
        subscriber = _get_subscriber()
        subscriber.pull(subscription=sub_path, max_messages=1, timeout=3)
        msg = f"Subscription {_temp_sub_name} verified healthy"
    except Exception as e:
        err_str = str(e)
        if "NOT_FOUND" in err_str or "does not exist" in err_str.lower() or "404" in err_str:
            _recreate_subscription()
            msg = f"Subscription recreated: {_temp_sub_name} (was NOT_FOUND)"
        elif "DEADLINE_EXCEEDED" in err_str or "504" in err_str:
            msg = f"Subscription {_temp_sub_name} healthy (no pending messages)"
        else:
            msg = f"Subscription {_temp_sub_name} pull error (non-fatal): {err_str[:100]}"
    logger.info(msg)
    return msg


def drain_subscription() -> int:
    """Pull and ack all pending messages from the subscription.

    Returns the number of messages drained. Use between runs to clear
    stale responses from failed workflow dispatches that would otherwise
    cause correlation_id mismatches and polling timeouts.
    """
    subscriber = _get_subscriber()
    sub_path = _results_sub()
    total_drained = 0

    for _ in range(20):
        try:
            response = subscriber.pull(
                subscription=sub_path,
                max_messages=100,
                timeout=3,
            )
        except Exception:
            break

        if not response.received_messages:
            break

        ack_ids = [m.ack_id for m in response.received_messages]
        count = len(ack_ids)
        subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)
        total_drained += count
        logger.info("Drained %d stale messages from subscription", count)

        if count < 100:
            break

    return total_drained


def reset_between_runs() -> dict:
    """Clean slate for the next workflow run.

    Drains stale Pub/Sub messages and clears the pending results cache.
    Does NOT delete/recreate the subscription — that causes a window
    where messages are lost. Only refreshes if the subscription is
    actually broken (pull fails).
    """
    with _pending_lock:
        stale_count = len(_pending_results)
        _pending_results.clear()
        _pending_acks.clear()

    drained = drain_subscription()

    # Verify the subscription still works — only refresh if broken
    sub_ok = True
    try:
        subscriber = _get_subscriber()
        sub_path = _results_sub()
        subscriber.pull(subscription=sub_path, max_messages=1, timeout=3)
    except Exception as e:
        if "NOT_FOUND" in str(e) or "does not exist" in str(e).lower():
            sub_ok = False
            logger.warning("Subscription broken, refreshing: %s", e)
            refresh_subscription()
        # DEADLINE_EXCEEDED is fine — just means no messages

    msg = (
        f"Reset complete: drained {drained} messages, "
        f"cleared {stale_count} cached results, sub_ok={sub_ok}"
    )
    logger.info(msg)
    return {"drained": drained, "stale_cleared": stale_count, "message": msg}


def _cleanup_orphaned_subscriptions() -> None:
    """Delete stale temp subscriptions left by crashed processes."""
    subscriber = _get_subscriber()
    project_path = f"projects/{config.GCP_PUBSUB_PROJECT_ID}"
    topic = _results_topic()
    prefix = config.PUBSUB_RESULTS_SUB + "-"

    for sub in subscriber.list_subscriptions(request={"project": project_path}):
        short_name = sub.name.split("/")[-1]
        if not short_name.startswith(prefix):
            continue
        if short_name == config.PUBSUB_RESULTS_SUB:
            continue
        if sub.name == _temp_sub_path:
            continue
        if sub.topic != topic:
            continue
        try:
            subscriber.delete_subscription(subscription=sub.name)
            logger.info("Cleaned up orphaned subscription: %s", short_name)
        except Exception as e:
            logger.warning("Failed to clean up %s: %s", short_name, e)


def _publish_command(command_type: str, payload: dict, correlation_id: str) -> None:
    """Publish a command message to the dci-commands topic."""
    message = {
        "correlation_id": correlation_id,
        "command_type": command_type,
        "session_id": get_session_id(),
        "payload": payload,
        "_heartbeat_capable": True,
    }
    data = json.dumps(message).encode("utf-8")

    block_error = usage_tracker.check_before_publish(len(data))
    if block_error:
        raise RuntimeError(block_error)

    publisher = _get_publisher()
    t0 = time.time()
    future = publisher.publish(_commands_topic(), data)
    future.result(timeout=30)
    pub_ms = round((time.time() - t0) * 1000)

    usage_tracker.record_published(len(data))
    logger.info("Published command %s (corr: %s, %d bytes, %dms)", command_type, correlation_id[:8], len(data), pub_ms)


def _poll_for_result(
    correlation_id: str,
    timeout: float,
    on_progress: callable = None,
    heartbeat_timeout: float = 120,
) -> dict | None:
    """
    Poll the dci-results subscription for a result matching correlation_id.

    Handles three message types from the relay:
    - "ack": relay received the command (resets heartbeat clock)
    - "heartbeat": relay is still processing (resets heartbeat clock)
    - "final" (or absent): the actual result — returned to caller

    If heartbeats were received but then stop for longer than heartbeat_timeout,
    returns a _relay_lost error instead of waiting for the full timeout.
    """
    subscriber = _get_subscriber()
    sub_path = _results_sub()
    deadline = time.time() + timeout
    last_heartbeat_time = None
    backoff = 1.0

    while time.time() < deadline:
        with _pending_lock:
            if correlation_id in _pending_results:
                return _pending_results.pop(correlation_id)

        try:
            response = subscriber.pull(
                subscription=sub_path,
                max_messages=10,
                timeout=min(30, max(1, deadline - time.time())),
            )
            _telemetry.record_pull_success()
            backoff = 1.0
        except Exception as e:
            err_str = str(e)
            if "DEADLINE_EXCEEDED" in err_str or "504" in err_str:
                _telemetry.record_pull_success()
                pass
            elif "NOT_FOUND" in err_str or "does not exist" in err_str.lower():
                logger.error("Subscription gone (NOT_FOUND), recreating")
                _recreate_subscription()
                sub_path = _results_sub()
                subscriber = _get_subscriber()
                continue
            else:
                _telemetry.record_pull_error(str(e))
                snap = _telemetry.snapshot()
                logger.error("Pub/Sub pull error #%d (backoff=%.0fs): %s", snap["pull_error_count"], backoff, e)
                if "401" in err_str or "403" in err_str or "transport" in err_str.lower():
                    _reset_clients()
                    subscriber = _get_subscriber()
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
            if last_heartbeat_time is not None:
                gap = time.time() - last_heartbeat_time
                if gap > heartbeat_timeout:
                    return _relay_lost_result(gap, heartbeat_timeout)
            continue

        ack_ids = []
        for received_message in response.received_messages:
            ack_ids.append(received_message.ack_id)
            raw_bytes = len(received_message.message.data)
            usage_tracker.record_received(raw_bytes)
            try:
                msg = json.loads(received_message.message.data.decode("utf-8"))
                msg_corr = msg.get("correlation_id", "")
                msg_type = msg.get("message_type", "final")

                if msg_corr == correlation_id:
                    if msg_type == "final":
                        _telemetry.record_relay_response()
                        if ack_ids:
                            subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)
                        return msg.get("result", {})
                    elif msg_type == "ack":
                        last_heartbeat_time = time.time()
                        logger.info("ACK received for %s", correlation_id[:8])
                        if on_progress:
                            on_progress("ack", msg)
                    elif msg_type == "heartbeat":
                        last_heartbeat_time = time.time()
                        hb = msg.get("heartbeat", {})
                        logger.info(
                            "Heartbeat #%d for %s: elapsed=%ds, phase=%s",
                            hb.get("seq", 0), correlation_id[:8],
                            hb.get("elapsed_seconds", 0), hb.get("phase", ""),
                        )
                        if on_progress:
                            on_progress("heartbeat", msg)
                else:
                    if msg_type == "final":
                        with _pending_lock:
                            _pending_results[msg_corr] = msg.get("result", {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Discarding malformed result message")

        if ack_ids:
            subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)

        if last_heartbeat_time is not None:
            gap = time.time() - last_heartbeat_time
            if gap > heartbeat_timeout:
                return _relay_lost_result(gap, heartbeat_timeout)

    return None


def _relay_lost_result(gap: float, threshold: float) -> dict:
    logger.error("Relay lost: no heartbeat for %.0fs (threshold: %.0fs)", gap, threshold)
    return {
        "success": False,
        "error": (
            f"Relay lost: no heartbeat received for {int(gap)}s "
            f"(threshold: {int(threshold)}s). The relay daemon may have crashed "
            "or lost network connectivity."
        ),
        "_relay_lost": True,
    }


def get_connection_diagnostics() -> dict:
    """Return diagnostic state for troubleshooting timeouts.

    Answers: is the subscription alive? when did we last pull successfully?
    when did the relay last respond? how many errors? This is what you look
    at when a tool call times out — each field points to a different root cause.
    """
    now = time.time()
    snap = _telemetry.snapshot()
    sub_age = round(now - _temp_sub_created_at) if _temp_sub_created_at > 0 else None
    last_pull_ago = round(now - snap["last_successful_pull"]) if snap["last_successful_pull"] > 0 else None
    last_relay_ago = round(now - snap["last_relay_response"]) if snap["last_relay_response"] > 0 else None

    diagnosis = "unknown"
    if _temp_sub_path is None:
        diagnosis = "SUBSCRIPTION_MISSING — no subscription created yet"
    elif last_pull_ago is not None and last_pull_ago > 120:
        diagnosis = f"PULL_STALE — last successful pull {last_pull_ago}s ago, subscription may be dead"
    elif last_relay_ago is not None and last_relay_ago > 300:
        diagnosis = f"RELAY_SILENT — relay last responded {last_relay_ago}s ago, may be down"
    elif snap["pull_error_count"] > 0:
        diagnosis = f"PULL_ERRORS — {snap['pull_error_count']} consecutive pull errors"
    else:
        diagnosis = "HEALTHY"

    result = {
        "subscription": _temp_sub_name,
        "subscription_age_seconds": sub_age,
        "last_successful_pull_seconds_ago": last_pull_ago,
        "last_relay_response_seconds_ago": last_relay_ago,
        "pull_error_count": snap["pull_error_count"],
        "messages_received_total": snap["messages_received_total"],
        "pending_results_cached": len(_pending_results),
        "diagnosis": diagnosis,
    }
    if snap["last_pull_error"]:
        result["last_pull_error"] = snap["last_pull_error"]
    return result


def check_pubsub_health() -> dict:
    """Fast local-only health check: verify credentials and Pub/Sub connectivity.

    Does NOT require the relay to be running — only checks that we can
    reach Google Cloud Pub/Sub with valid credentials. Returns a dict
    with 'healthy' bool, 'error' string if unhealthy, and 'details' for diagnostics.
    """
    import os as _os

    details = []
    try:
        sa_path = _os.environ.get("PUBSUB_SA_KEY_PATH", "") or _os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        details.append(f"SA key path: {sa_path or 'NOT SET'}")
        details.append(f"SA key exists: {bool(sa_path and _os.path.exists(sa_path))}")

        creds = _get_pubsub_credentials()
        if creds is None:
            details.append("Credentials: NONE — no SA key file found")
            return {
                "healthy": False,
                "error": "No Pub/Sub credentials found. Set PUBSUB_SA_KEY_PATH in .env.",
                "details": details,
            }

        details.append(f"Credentials project: {getattr(creds, 'project_id', 'unknown')}")

        _get_publisher()
        topic = _commands_topic()
        details.append(f"Topic: {topic}")
        details.append("Publisher client: OK")

        return {"healthy": True, "error": "", "details": details}
    except Exception as e:
        err = str(e)
        details.append(f"Error: {err[:300]}")
        if "403" in err or "SERVICE_DISABLED" in err:
            return {
                "healthy": False,
                "error": f"Pub/Sub credentials rejected by Google Cloud: {err[:200]}. "
                         "Check that PUBSUB_SA_KEY_PATH points to the correct service account key.",
                "details": details,
            }
        if "404" in err:
            return {
                "healthy": False,
                "error": f"Pub/Sub topic not found: {err[:200]}. "
                         "Check pubsub_commands_topic in run_config.yml.",
                "details": details,
            }
        return {
            "healthy": False,
            "error": f"Pub/Sub health check failed: {err[:300]}",
            "details": details,
        }


async def send_command(
    command_type: str,
    payload: dict,
    timeout: float = 120,
    on_progress: callable = None,
    heartbeat_timeout: float = 120,
) -> dict:
    """
    Send a command to the relay via Pub/Sub and wait for the result.

    Checks usage against the free tier before publishing. Blocks if at 95%.
    Every response includes a _diagnostics list showing what happened at each
    step so connection issues are visible in the tool output.

    on_progress: called with (message_type, msg_dict) for ACK and heartbeat messages.
    heartbeat_timeout: if heartbeats were received but then stop for this many
        seconds, return a _relay_lost error instead of waiting for the full timeout.
    """
    import time as _time

    correlation_id = str(uuid.uuid4())
    diag = []

    diag.append(f"[1/4] Command: {command_type}, correlation: {correlation_id[:8]}")
    diag.append(f"[2/4] Pub/Sub topic: {_commands_topic()}")

    logger.info("Sending command %s (corr: %s)", command_type, correlation_id[:8])

    loop = asyncio.get_running_loop()

    await loop.run_in_executor(_executor, _results_sub)

    try:
        t0 = _time.time()
        await loop.run_in_executor(_executor, _publish_command, command_type, payload, correlation_id)
        publish_ms = int((_time.time() - t0) * 1000)
        diag.append(f"[3/4] Published to Pub/Sub OK ({publish_ms}ms)")
    except RuntimeError as e:
        diag.append(f"[3/4] PUBLISH FAILED: {e}")
        return {"success": False, "error": str(e), "_diagnostics": diag}

    diag.append(f"[4/4] Polling for relay response (timeout={timeout}s)...")

    poll_fn = functools.partial(
        _poll_for_result,
        correlation_id,
        timeout,
        on_progress=on_progress,
        heartbeat_timeout=heartbeat_timeout,
    )

    t0 = _time.time()
    result = await loop.run_in_executor(_executor, poll_fn)
    poll_s = round(_time.time() - t0, 1)

    if result is None:
        drained = drain_subscription()
        if drained > 0:
            logger.info("Timeout: drained %d stale messages, retrying poll (15s)", drained)
            diag.append(f"[4/4] Timeout after {poll_s}s — drained {drained} stale messages, retrying...")
            retry_fn = functools.partial(
                _poll_for_result, correlation_id, 15,
                on_progress=on_progress, heartbeat_timeout=heartbeat_timeout,
            )
            result = await loop.run_in_executor(_executor, retry_fn)

        if result is None:
            conn_diag = get_connection_diagnostics()
            diag.append(f"[4/4] NO RESPONSE after {poll_s}s — {conn_diag['diagnosis']}")
            return {
                "success": False,
                "error": f"Timeout after {poll_s}s. Diagnosis: {conn_diag['diagnosis']}. "
                         f"Sub: {conn_diag['subscription']}, "
                         f"last_pull: {conn_diag['last_successful_pull_seconds_ago']}s ago, "
                         f"last_relay: {conn_diag['last_relay_response_seconds_ago']}s ago, "
                         f"pull_errors: {conn_diag['pull_error_count']}",
                "_diagnostics": diag,
                "_connection_state": conn_diag,
            }

    if isinstance(result, dict) and result.get("_relay_lost"):
        diag.append(f"[4/4] RELAY LOST — heartbeats stopped after {poll_s}s")
        result["_diagnostics"] = diag
        return result

    diag.append(f"[4/4] Response received from relay ({poll_s}s, success={result.get('success', 'N/A')})")
    logger.info("Received result for %s: success=%s", command_type, result.get("success", "N/A"))
    if isinstance(result, dict):
        result["_diagnostics"] = diag
    return result


# [AGENT-ADDED] Non-blocking command start: publish and wait for ACK only
async def send_command_start(
    command_type: str,
    payload: dict,
    ack_timeout: float = 60,
) -> dict:
    """Publish a command and wait for the relay's ACK, then return immediately.

    Unlike send_command() which blocks until the final result, this returns
    as soon as the relay acknowledges receipt. The caller uses check_for_result()
    to poll for the final result later.

    Returns dict with correlation_id and ack_received status.
    """
    import time as _time

    correlation_id = str(uuid.uuid4())
    diag = []

    diag.append(f"[1/3] Command: {command_type}, correlation: {correlation_id[:8]}")
    diag.append(f"[2/3] Pub/Sub topic: {_commands_topic()}")

    logger.info("Starting command %s (corr: %s, ACK-only)", command_type, correlation_id[:8])

    loop = asyncio.get_running_loop()

    await loop.run_in_executor(_executor, _results_sub)

    try:
        t0 = _time.time()
        await loop.run_in_executor(
            _executor, _publish_command, command_type, payload, correlation_id,
        )
        publish_ms = int((_time.time() - t0) * 1000)
        diag.append(f"[3/3] Published to Pub/Sub OK ({publish_ms}ms)")
    except RuntimeError as e:
        diag.append(f"[3/3] PUBLISH FAILED: {e}")
        return {"correlation_id": correlation_id, "ack_received": False,
                "success": False, "error": str(e), "_diagnostics": diag}

    diag.append(f"[3/3] Waiting for relay ACK (timeout={ack_timeout}s)...")

    wait_fn = functools.partial(_wait_for_ack, correlation_id, ack_timeout)
    t0 = _time.time()
    ack_result = await loop.run_in_executor(_executor, wait_fn)
    wait_s = round(_time.time() - t0, 1)

    if ack_result:
        diag.append(f"[3/3] ACK received from relay ({wait_s}s)")
        return {"correlation_id": correlation_id, "ack_received": True,
                "success": True, "_diagnostics": diag}
    else:
        diag.append(f"[3/3] NO ACK after {wait_s}s — relay may be down")
        return {"correlation_id": correlation_id, "ack_received": False,
                "success": False,
                "error": f"No ACK from relay after {wait_s}s. "
                         "The relay daemon may be down or unreachable.",
                "_diagnostics": diag}


def _wait_for_ack(correlation_id: str, timeout: float) -> bool:
    """Poll Pub/Sub until an ACK message arrives for the given correlation_id."""
    subscriber = _get_subscriber()
    sub_path = _results_sub()
    deadline = time.time() + timeout
    backoff = 1.0

    while time.time() < deadline:
        # [AGENT-ADDED] Check if background poller already captured the ACK
        with _pending_lock:
            if correlation_id in _pending_acks:
                _pending_acks.pop(correlation_id)
                logger.info("ACK found in pending cache for %s", correlation_id[:8])
                return True

        try:
            response = subscriber.pull(
                subscription=sub_path,
                max_messages=10,
                timeout=min(10, max(1, deadline - time.time())),
            )
            backoff = 1.0
        except Exception as e:
            if "DEADLINE_EXCEEDED" not in str(e) and "504" not in str(e):
                logger.error("Pub/Sub pull error during ACK wait (backoff=%.0fs): %s", backoff, e)
                if "401" in str(e) or "403" in str(e) or "transport" in str(e).lower():
                    _reset_clients()
                    subscriber = _get_subscriber()
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
            continue

        ack_ids = []
        got_ack = False
        for received_message in response.received_messages:
            ack_ids.append(received_message.ack_id)
            raw_bytes = len(received_message.message.data)
            usage_tracker.record_received(raw_bytes)
            try:
                msg = json.loads(received_message.message.data.decode("utf-8"))
                msg_corr = msg.get("correlation_id", "")
                msg_type = msg.get("message_type", "final")

                if msg_corr == correlation_id and msg_type == "ack":
                    got_ack = True
                elif msg_corr == correlation_id and msg_type == "final":
                    with _pending_lock:
                        _pending_results[msg_corr] = msg.get("result", {})
                    got_ack = True
                elif msg_type == "final":
                    with _pending_lock:
                        _pending_results[msg_corr] = msg.get("result", {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Discarding malformed message during ACK wait")

        if ack_ids:
            subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)

        if got_ack:
            return True

    return False


# [AGENT-ADDED] Non-blocking result check for a previously started command
async def check_for_result(
    correlation_id: str,
    poll_seconds: float = 5,
) -> dict:
    """Non-blocking single-pass poll for a result matching correlation_id.

    Returns immediately if result is already cached. Otherwise does one
    short Pub/Sub pull. Call this periodically (every 60-90s) to check
    on a workflow started with send_command_start().

    Returns:
        {"status": "completed", "result": {...}} — final result found
        {"status": "running", "last_heartbeat": {...}} — still in progress
        {"status": "unknown"} — no messages found in this poll cycle
    """
    with _pending_lock:
        if correlation_id in _pending_results:
            result = _pending_results.pop(correlation_id)
            return {"status": "completed", "result": result}

    loop = asyncio.get_running_loop()
    poll_fn = functools.partial(_single_poll, correlation_id, poll_seconds)
    return await loop.run_in_executor(_executor, poll_fn)


def _single_poll(correlation_id: str, poll_seconds: float) -> dict:
    """One-shot Pub/Sub pull looking for a specific correlation_id."""
    subscriber = _get_subscriber()
    sub_path = _results_sub()
    last_heartbeat = None

    try:
        response = subscriber.pull(
            subscription=sub_path,
            max_messages=10,
            timeout=poll_seconds,
        )
    except Exception as e:
        if "DEADLINE_EXCEEDED" in str(e) or "504" in str(e):
            return {"status": "unknown"}
        logger.error("Pub/Sub pull error during result check: %s", e)
        if "401" in str(e) or "403" in str(e) or "transport" in str(e).lower():
            _reset_clients()
        return {"status": "unknown"}

    ack_ids = []
    for received_message in response.received_messages:
        ack_ids.append(received_message.ack_id)
        raw_bytes = len(received_message.message.data)
        usage_tracker.record_received(raw_bytes)
        try:
            msg = json.loads(received_message.message.data.decode("utf-8"))
            msg_corr = msg.get("correlation_id", "")
            msg_type = msg.get("message_type", "final")

            if msg_corr == correlation_id:
                if msg_type == "final":
                    if ack_ids:
                        subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)
                    return {"status": "completed", "result": msg.get("result", {})}
                elif msg_type == "heartbeat":
                    last_heartbeat = msg.get("heartbeat", {})
            else:
                if msg_type == "final":
                    with _pending_lock:
                        _pending_results[msg_corr] = msg.get("result", {})
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Discarding malformed message during result check")

    if ack_ids:
        subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)

    if last_heartbeat:
        return {"status": "running", "last_heartbeat": last_heartbeat}

    return {"status": "unknown"}


def notify_relay_update() -> None:
    """Fire-and-forget: tell the relay to git pull (no response wait).

    Called automatically after every successful git push so the relay
    stays in sync without manual intervention.
    """
    correlation_id = str(uuid.uuid4())
    try:
        _publish_command("relay.update", {}, correlation_id)
        logger.info("Notified relay to pull latest (corr: %s)", correlation_id[:8])
    except Exception as e:
        logger.warning("Failed to notify relay of update (non-fatal): %s", e)


def _background_poll_loop():
    """Daemon thread: continuously pull Pub/Sub for final/heartbeat messages."""
    backoff = 1
    _CIRCUIT_BREAKER_THRESHOLD = 10
    _WATCHDOG_STALE_SECONDS = 300
    while not _bg_poller_stop.is_set():
        if _telemetry.is_stale(_WATCHDOG_STALE_SECONDS):
            logger.warning("Watchdog: no successful pull in %ds, forcing full reset", _WATCHDOG_STALE_SECONDS)
            _reset_clients()
            _recreate_subscription()
            _telemetry.reset_errors()
            backoff = 1

        try:
            subscriber = _get_subscriber()
            sub_path = _results_sub()
            response = subscriber.pull(
                subscription=sub_path,
                max_messages=10,
                timeout=30,
            )
        except Exception as e:
            if "DEADLINE_EXCEEDED" in str(e) or "504" in str(e):
                _telemetry.record_pull_success()
                continue
            logger.warning("Background poller pull error: %s", e)
            _telemetry.record_pull_error(str(e))
            if "401" in str(e) or "403" in str(e) or "transport" in str(e).lower():
                _reset_clients()
            if _telemetry.error_count_exceeds(_CIRCUIT_BREAKER_THRESHOLD):
                snap = _telemetry.snapshot()
                logger.warning("Circuit breaker: %d consecutive errors, recreating subscription", snap["pull_error_count"])
                _reset_clients()
                _recreate_subscription()
                _telemetry.reset_errors()
                backoff = 1
            else:
                _bg_poller_stop.wait(min(backoff, 30))
                backoff = min(backoff * 2, 30)
            continue

        backoff = 1
        ack_ids = []
        for received_message in response.received_messages:
            ack_ids.append(received_message.ack_id)
            raw_bytes = len(received_message.message.data)
            usage_tracker.record_received(raw_bytes)
            try:
                msg = json.loads(received_message.message.data.decode("utf-8"))
                msg_corr = msg.get("correlation_id", "")
                msg_type = msg.get("message_type", "final")

                _telemetry.record_full_success()

                if msg_type == "final":
                    result = msg.get("result", {})
                    with _pending_lock:
                        _pending_results[msg_corr] = result
                    logger.info("Background poller: captured completion for %s", msg_corr[:8])
                    for cb in _completion_callbacks:
                        try:
                            cb(msg_corr, result)
                        except Exception as cb_err:
                            logger.warning("Completion callback error: %s", cb_err)
                elif msg_type == "heartbeat":
                    hb = msg.get("heartbeat", {})
                    with _heartbeat_lock:
                        _latest_heartbeats[msg_corr] = hb
                    for cb in _heartbeat_callbacks:
                        try:
                            cb(msg_corr, hb)
                        except Exception as cb_err:
                            logger.warning("Heartbeat callback error: %s", cb_err)
                elif msg_type == "ack":
                    # [AGENT-ADDED] Store ACK so _wait_for_ack can find it
                    with _pending_lock:
                        _pending_acks[msg_corr] = True
                    logger.info("Background poller: captured ACK for %s", msg_corr[:8])
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Background poller: discarding malformed message")

        if ack_ids:
            try:
                subscriber = _get_subscriber()
                subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)
            except Exception as e:
                logger.warning("Background poller: ack failed: %s", e)


def register_completion_callback(fn):
    """Register a callback invoked when a workflow completes. fn(correlation_id, result)."""
    _completion_callbacks.append(fn)


def register_heartbeat_callback(fn):
    """Register a callback invoked on each heartbeat. fn(correlation_id, heartbeat)."""
    _heartbeat_callbacks.append(fn)


def start_completion_poller():
    """Start the background Pub/Sub poller thread (idempotent)."""
    global _bg_poller_thread
    if _bg_poller_thread is not None and _bg_poller_thread.is_alive():
        return
    _bg_poller_stop.clear()
    _bg_poller_thread = threading.Thread(
        target=_background_poll_loop, name="bg-completion-poller", daemon=True
    )
    _bg_poller_thread.start()
    logger.info("Background completion poller started")


def stop_completion_poller():
    """Stop the background poller thread."""
    global _bg_poller_thread
    if _bg_poller_thread is None:
        return
    _bg_poller_stop.set()
    _bg_poller_thread.join(timeout=5)
    _bg_poller_thread = None
    logger.info("Background completion poller stopped")


def get_latest_heartbeat(correlation_id: str) -> dict | None:
    """Return the most recent heartbeat for a correlation_id, or None."""
    with _heartbeat_lock:
        return _latest_heartbeats.get(correlation_id)


def pop_pending_completions(correlation_ids: list[str]) -> dict[str, dict]:
    """Check and pop any completed results for the given correlation_ids."""
    found = {}
    with _pending_lock:
        for cid in correlation_ids:
            if cid in _pending_results:
                found[cid] = _pending_results.pop(cid)
    return found


def close():
    """Close Pub/Sub clients, delete temp subscription, and print usage summary."""
    stop_completion_poller()
    _cleanup_temp_subscription()

    summary = usage_tracker.get_usage_summary()
    logger.info(
        "Pub/Sub usage this month: %.2f MB / %.0f MB (%.4f%%) - %s",
        summary["total_mb"], summary["free_tier_mb"],
        summary["usage_pct"], summary["status"],
    )

    _reset_clients()
    _executor.shutdown(wait=False)
