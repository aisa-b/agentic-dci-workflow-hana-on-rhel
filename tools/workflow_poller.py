#!/usr/bin/env python3
"""Poll the relay's workflow.list every 2 minutes and write status to a local JSON file.

Usage:
    python3 tools/workflow_poller.py [--interval 120] [--output .claude/workflow_status.json]

The agent reads .claude/workflow_status.json to get reliable phase/task info
instead of guessing from process names on the jumpbox.

PBO benchmark output (phase 4) is truncated to the last 10 lines to avoid
bloating the JSON and Pub/Sub memory.
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

project_root = Path(__file__).resolve().parent.parent
env_file = project_root / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)

from google.cloud import pubsub_v1
from google.api_core.exceptions import AlreadyExists
from google.oauth2 import service_account
from google.protobuf.duration_pb2 import Duration

from agents import config
from config_loader import load_run_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("workflow_poller")

PHASE_NAMES = {
    1: "OS Deployment",
    2: "OS Prep for HANA",
    3: "HANA Installation",
    4: "PBO Benchmark",
    5: "Results",
}

PHASE_PATTERNS = {
    1: [r"deploy", r"kickstart", r"pxe", r"install.*rhel", r"os.*deploy",
        r"provision", r"bare.?metal"],
    2: [r"sap.preconfigure", r"sap.hana.preconfigure", r"tuned", r"kernel.*param",
        r"prep.*hana", r"os.*prep", r"sap.*prep"],
    3: [r"hdblcm", r"hana.*install", r"install.*hana", r"saphana",
        r"hana.*setup", r"database.*install"],
    4: [r"pboffline", r"pbo", r"benchmark", r"perf.*bench"],
    5: [r"result", r"report", r"upload", r"junit", r"collect.*result"],
}

_COMPILED = {
    phase: [re.compile(p, re.IGNORECASE) for p in patterns]
    for phase, patterns in PHASE_PATTERNS.items()
}


def detect_phase(phase_str: str) -> tuple[int | None, str]:
    """Map relay phase string to (phase_number, phase_name)."""
    if not phase_str:
        return None, ""
    text = phase_str.lower()
    for phase, patterns in _COMPILED.items():
        for pat in patterns:
            if pat.search(text):
                return phase, PHASE_NAMES[phase]
    return None, ""


def extract_task_name(phase_str: str) -> str:
    """Extract the task name from 'task:some task name' or 'play:some play'."""
    if ":" in phase_str:
        return phase_str.split(":", 1)[1].strip()
    return phase_str


def truncate_pbo_output(line: str, max_lines: int = 10) -> str:
    """For PBO phase, keep only the last N lines."""
    lines = line.strip().splitlines()
    if len(lines) <= max_lines:
        return line.strip()
    return "\n".join(lines[-max_lines:])


def elapsed_human(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def get_credentials():
    sa_key = os.environ.get("PUBSUB_SA_KEY_PATH", "") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    if sa_key and os.path.exists(sa_key):
        return service_account.Credentials.from_service_account_file(
            sa_key, scopes=["https://www.googleapis.com/auth/pubsub"]
        )
    return None


def _load_server_fqdns() -> dict[str, str]:
    """Load hostname → fqdn mapping from run_config.yml."""
    rc = load_run_config()
    fqdns = {}
    for hostname, info in rc.get("servers", {}).items():
        if isinstance(info, dict) and info.get("fqdn"):
            fqdns[hostname] = info["fqdn"]
    return fqdns


PHASE_TIMINGS_FILE = config.LOG_DIR / "phase_timings.json"
PHASE_TRACKER_STATE = Path(".claude/phase_tracker_state.json")


class WorkflowPoller:
    def __init__(self, output_path: str, interval: int = 120):
        self.output_path = Path(output_path)
        self.interval = interval
        self.project_id = config.GCP_PUBSUB_PROJECT_ID
        self.commands_topic = f"projects/{self.project_id}/topics/{config.PUBSUB_COMMANDS_TOPIC}"
        self.results_topic = f"projects/{self.project_id}/topics/{config.PUBSUB_RESULTS_TOPIC}"

        creds = get_credentials()
        self.publisher = pubsub_v1.PublisherClient(credentials=creds) if creds else pubsub_v1.PublisherClient()
        self.subscriber = pubsub_v1.SubscriberClient(credentials=creds) if creds else pubsub_v1.SubscriberClient()

        self._session_id = str(uuid.uuid4())
        self._sub_path = None
        self._running = True
        self._server_fqdns = _load_server_fqdns()
        self._phase_tracker: dict[str, dict] = self._load_tracker_state()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info("Signal %d received, shutting down...", signum)
        self._running = False

    def _ensure_subscription(self) -> str:
        if self._sub_path:
            return self._sub_path

        sub_name = "dci-results-poller"
        sub_path = f"projects/{self.project_id}/subscriptions/{sub_name}"

        try:
            self.subscriber.create_subscription(
                request={
                    "name": sub_path,
                    "topic": self.results_topic,
                    "ack_deadline_seconds": 30,
                    "expiration_policy": {"ttl": Duration(seconds=86400)},
                },
            )
            logger.info("Created subscription: %s", sub_name)
        except AlreadyExists:
            logger.debug("Reusing subscription: %s", sub_name)

        self._sub_path = sub_path
        return sub_path

    @staticmethod
    def _load_tracker_state() -> dict[str, dict]:
        """Load persisted phase tracker state from disk."""
        if not PHASE_TRACKER_STATE.exists():
            return {}
        try:
            return json.loads(PHASE_TRACKER_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_tracker_state(self):
        """Persist phase tracker state to disk atomically."""
        PHASE_TRACKER_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PHASE_TRACKER_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._phase_tracker, indent=2) + "\n")
        tmp.rename(PHASE_TRACKER_STATE)

    def _get_topic_for_host(self, target_host: str) -> str:
        """Look up the RHEL topic from the tracker state (set at dispatch time)."""
        tracker = self._phase_tracker.get(target_host, {})
        return tracker.get("topic", "")

    def _track_phase(self, target_host: str, phase_num: int | None, total_elapsed: int):
        """Track phase transitions for a target. Records duration when phase changes."""
        if phase_num is None:
            return

        tracker = self._phase_tracker.get(target_host)
        if not tracker:
            self._phase_tracker[target_host] = {
                "current_phase": phase_num,
                "phase_start_time": time.time(),
                "workflow_start_elapsed": total_elapsed,
                "durations": {},
            }
            return

        if phase_num != tracker["current_phase"]:
            duration = int(time.time() - tracker["phase_start_time"])
            tracker["durations"][str(tracker["current_phase"])] = duration
            logger.info(
                "Phase transition on %s: %d → %d (phase %d took %ds)",
                target_host.split(".")[0], tracker["current_phase"], phase_num,
                tracker["current_phase"], duration,
            )
            tracker["current_phase"] = phase_num
            tracker["phase_start_time"] = time.time()

    def _elapsed_in_current_phase(self, target_host: str) -> int:
        """Return seconds spent in the current phase."""
        tracker = self._phase_tracker.get(target_host)
        if not tracker:
            return 0
        return int(time.time() - tracker["phase_start_time"])

    def _finalize_workflow(self, target_host: str, success: bool, total_elapsed: int):
        """Record final phase duration and update running averages in phase_timings.json.

        File structure:
        {
          "target-1:RHEL-10.2": {
            "run_count": 5,
            "phase_averages": {"1": 1800, "2": 900, "3": 1200, "4": 3900, "5": 300}
          }
        }
        """
        tracker = self._phase_tracker.pop(target_host, None)
        if not tracker:
            return

        duration = int(time.time() - tracker["phase_start_time"])
        tracker["durations"][str(tracker["current_phase"])] = duration

        topic = self._get_topic_for_host(target_host)
        short_host = target_host.split(".")[0]
        key = f"{short_host}:{topic}"

        try:
            PHASE_TIMINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            db = {}
            if PHASE_TIMINGS_FILE.exists():
                db = json.loads(PHASE_TIMINGS_FILE.read_text())

            entry = db.get(key, {"run_count": 0, "phase_averages": {}})
            n = entry["run_count"]
            avgs = entry["phase_averages"]

            for phase_str, dur in tracker["durations"].items():
                old_avg = avgs.get(phase_str, 0)
                avgs[phase_str] = round((old_avg * n + dur) / (n + 1))

            entry["run_count"] = n + 1
            entry["phase_averages"] = avgs
            entry["last_updated"] = datetime.now(timezone.utc).isoformat()
            db[key] = entry

            tmp = PHASE_TIMINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(db, indent=2) + "\n")
            tmp.rename(PHASE_TIMINGS_FILE)

            logger.info(
                "Updated phase averages for %s (run %d): %s",
                key, n + 1, avgs,
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to update phase timings: %s", e)

    def _get_phase_timing(self, target_host: str, phase_num: int | None) -> dict:
        """Get expected timing for a phase from averaged data or static defaults."""
        if phase_num is None or phase_num not in PHASE_NAMES:
            return {}

        from agents.local.phase_expectations import PHASE_EXPECTATIONS
        static_exp = PHASE_EXPECTATIONS.get(phase_num, {})
        defaults = {
            "typical_minutes": static_exp.get("typical_duration_minutes", 30),
            "max_minutes": static_exp.get("max_duration_minutes", 60),
            "source": "static_default",
        }

        if not PHASE_TIMINGS_FILE.exists():
            return defaults

        topic = self._get_topic_for_host(target_host)
        short_host = target_host.split(".")[0]
        key = f"{short_host}:{topic}"
        phase_key = str(phase_num)

        try:
            db = json.loads(PHASE_TIMINGS_FILE.read_text())
            entry = db.get(key)
            if not entry or entry.get("run_count", 0) < 3:
                defaults["data_points"] = entry["run_count"] if entry else 0
                return defaults

            avg_secs = entry["phase_averages"].get(phase_key)
            if avg_secs is None:
                return defaults

            avg_min = avg_secs / 60.0
            return {
                "typical_minutes": round(avg_min, 1),
                "max_minutes": round(avg_min * 1.3, 1),
                "source": f"learned ({entry['run_count']} runs)",
                "data_points": entry["run_count"],
            }
        except (json.JSONDecodeError, OSError, KeyError):
            return defaults

    def _send_and_receive(self, command_type: str, payload: dict, timeout: float = 30) -> dict | None:
        """Publish a command and wait for the relay's response."""
        correlation_id = str(uuid.uuid4())
        sub_path = self._ensure_subscription()

        message = {
            "correlation_id": correlation_id,
            "command_type": command_type,
            "session_id": self._session_id,
            "payload": payload,
        }
        data = json.dumps(message).encode("utf-8")
        future = self.publisher.publish(self.commands_topic, data)
        future.result(timeout=10)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                response = self.subscriber.pull(
                    subscription=sub_path,
                    max_messages=10,
                    timeout=min(10, max(1, deadline - time.time())),
                )
            except Exception as e:
                if "DEADLINE_EXCEEDED" not in str(e) and "504" not in str(e):
                    logger.warning("Pull error: %s", e)
                continue

            ack_ids = []
            result = None
            for msg in response.received_messages:
                ack_ids.append(msg.ack_id)
                try:
                    parsed = json.loads(msg.message.data.decode("utf-8"))
                    if parsed.get("correlation_id") == correlation_id:
                        msg_type = parsed.get("message_type", "final")
                        if msg_type == "final":
                            result = parsed.get("result", {})
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            if ack_ids:
                self.subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)

            if result is not None:
                return result

        logger.warning("Timeout waiting for %s response", command_type)
        return None

    def poll_once(self) -> dict:
        """Query the relay and return structured status."""
        result = self._send_and_receive("workflow.list", {})
        if not result or not result.get("success"):
            return {
                "poll_time": datetime.now(timezone.utc).isoformat(),
                "error": result.get("error", "No response from relay") if result else "No response from relay",
                "workflows": [],
                "completed": [],
            }

        active_hosts = set()
        workflows = []
        for rw in result.get("workflows", []):
            host = rw.get("target_host", "")
            active_hosts.add(host)
            phase_str = rw.get("last_phase", "")
            phase_num, phase_name = detect_phase(phase_str)
            task = extract_task_name(phase_str)
            output_line = rw.get("last_output_line", "")
            elapsed = int(rw.get("running_seconds", 0))

            self._track_phase(host, phase_num, elapsed)

            if phase_num == 4:
                output_line = truncate_pbo_output(output_line)

            phase_elapsed = self._elapsed_in_current_phase(host)
            timing = self._get_phase_timing(host, phase_num)

            workflows.append({
                "target_host": host,
                "phase": phase_num,
                "phase_name": phase_name or "Unknown",
                "task": task,
                "last_output_line": output_line,
                "elapsed_seconds": elapsed,
                "elapsed_human": elapsed_human(elapsed),
                "elapsed_in_phase_seconds": phase_elapsed,
                "elapsed_in_phase_human": elapsed_human(phase_elapsed),
                "expected_typical_minutes": timing.get("typical_minutes"),
                "expected_max_minutes": timing.get("max_minutes"),
                "timing_source": timing.get("source", "static_default"),
                "correlation_id": rw.get("correlation_id", ""),
                "heartbeat_age": rw.get("last_heartbeat_age", 0),
            })

        completed = []
        for cw in result.get("completed", []):
            host = cw.get("target_host", "")
            c_elapsed = cw.get("elapsed_seconds", 0)
            if host in self._phase_tracker:
                self._finalize_workflow(host, cw.get("success", False), c_elapsed)
            completed.append({
                "target_host": host,
                "success": cw.get("success", False),
                "elapsed_seconds": c_elapsed,
                "elapsed_human": elapsed_human(c_elapsed),
                "error_summary": cw.get("error_summary", ""),
                "age_seconds": cw.get("age_seconds", 0),
            })

        self._save_tracker_state()

        return {
            "poll_time": datetime.now(timezone.utc).isoformat(),
            "workflows": workflows,
            "completed": completed,
            "relay_uptime": result.get("relay_uptime_seconds"),
        }

    def write_status(self, status: dict):
        """Write status to JSON file atomically."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.output_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2) + "\n")
        tmp.rename(self.output_path)

    def print_dashboard(self, status: dict):
        """Print a compact dashboard line."""
        ts = datetime.now().strftime("%H:%M")
        wfs = status.get("workflows", [])
        completed = status.get("completed", [])

        if not wfs and not completed:
            print(f"[{ts}] No active workflows")
            return

        for wf in wfs:
            phase = wf.get("phase") or "?"
            phase_name = wf.get("phase_name", "")
            task = wf.get("task", "")
            host = wf.get("target_host", "").split(".")[0]
            elapsed = wf.get("elapsed_human", "")
            phase_elapsed = wf.get("elapsed_in_phase_human", "")
            typical = wf.get("expected_typical_minutes")
            max_m = wf.get("expected_max_minutes")
            source = wf.get("timing_source", "")
            line = wf.get("last_output_line", "")[:80]

            timing_info = ""
            if typical and max_m:
                timing_info = f" (typical: {typical}m, max: {max_m}m — {source})"

            print(f"[{ts}] {host} — Phase {phase} ({phase_name}), {phase_elapsed} in phase, {elapsed} total{timing_info} | {task}")
            if line:
                print(f"        └─ {line}")

        for cw in completed:
            host = cw.get("target_host", "").split(".")[0]
            ok = "SUCCESS" if cw.get("success") else "FAILED"
            elapsed = cw.get("elapsed_human", "")
            err = cw.get("error_summary", "")
            msg = f"[{ts}] {host} — {ok} ({elapsed})"
            if err:
                msg += f" | {err[:80]}"
            print(msg)

    def run(self):
        """Main loop: poll every interval seconds until no workflows remain or stopped."""
        logger.info("Starting workflow poller (interval=%ds, output=%s)", self.interval, self.output_path)
        consecutive_empty = 0

        while self._running:
            try:
                status = self.poll_once()
                self.write_status(status)
                self.print_dashboard(status)

                if not status.get("workflows") and not status.get("error"):
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        logger.info("No active workflows for %d polls, exiting", consecutive_empty)
                        break
                else:
                    consecutive_empty = 0

            except Exception as e:
                logger.error("Poll error: %s", e)

            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Poller stopped")


def main():
    parser = argparse.ArgumentParser(description="Poll DCI workflow status from relay")
    parser.add_argument("--interval", type=int, default=120, help="Poll interval in seconds (default: 120)")
    parser.add_argument("--output", default=".claude/workflow_status.json", help="Output JSON file path")
    args = parser.parse_args()

    if not config.GCP_PUBSUB_PROJECT_ID:
        print("ERROR: GCP_PUBSUB_PROJECT_ID not set", file=sys.stderr)
        sys.exit(1)

    poller = WorkflowPoller(args.output, args.interval)
    poller.run()


if __name__ == "__main__":
    main()
