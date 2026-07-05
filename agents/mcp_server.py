"""
MCP server exposing DCI relay operations as tools for Claude Code.

Runs as a local stdio server. Each tool wraps a Pub/Sub command to the
relay daemon, which forwards it to the jumpbox/target.
"""

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from mcp.server.fastmcp import FastMCP

import time

from agents.bridge import pubsub_client as bridge
from agents.local import relay_kb
from agents.local import phase_timings

logger = logging.getLogger(__name__)

# [AGENT-ADDED] In-flight workflow tracker for non-blocking workflow runs
_inflight_workflows: dict[str, dict] = {}  # keyed by target_host
_inflight_lock = asyncio.Lock()
_INFLIGHT_FILE = Path(__file__).resolve().parent.parent / "inflight_workflows.json"

# [AGENT-ADDED] Background poller callback state (thread-safe, used from poller thread)
import threading as _threading
_corr_to_target: dict[str, str] = {}
_corr_lock = _threading.Lock()
_phase_start_times: dict[str, dict[int, float]] = {}
_stuck_alerted: set[tuple[str, int]] = set()


def _persist_inflight() -> None:
    """Save inflight workflows to disk so tracking survives MCP restarts."""
    try:
        serializable = {}
        for k, v in _inflight_workflows.items():
            serializable[k] = {k2: v2 for k2, v2 in v.items() if k2 != "result"}
        _INFLIGHT_FILE.write_text(json.dumps(serializable, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to persist inflight state: %s", e)


def _load_inflight() -> None:
    """Reload inflight workflows from disk on startup."""
    global _inflight_workflows
    if not _INFLIGHT_FILE.exists():
        return
    try:
        data = json.loads(_INFLIGHT_FILE.read_text())
        for k, v in data.items():
            if v.get("status") == "running":
                v["result"] = None
                _inflight_workflows[k] = v
        if _inflight_workflows:
            logger.info("Restored %d inflight workflow(s) from disk", len(_inflight_workflows))
    except Exception as e:
        logger.warning("Failed to load inflight state: %s", e)


async def _monitoring_checklist(target_host: str) -> dict:
    """Structured liveness checklist when heartbeat is stale (>10min).

    Runs 4 deterministic checks from the jumpbox side and returns a
    summary with overall alive/dead verdict plus per-check details.
    """
    short = target_host.split(".")[0] if target_host else ""
    checks = []
    alive = True

    async def _jb_cmd(cmd: str) -> str:
        try:
            r = await bridge.send_command("jumpbox.execute", {"command": cmd}, timeout=120)
            return r.get("stdout", "")
        except Exception:
            return ""

    # 1. dci-rhel-agent-ctl process
    ctl_out = await _jb_cmd("ps aux | grep dci-rhel-agent-ctl | grep -v grep")
    ctl_running = "dci-rhel-agent-ctl" in ctl_out
    checks.append({"check": "dci-rhel-agent-ctl", "ok": ctl_running,
                    "detail": "Running" if ctl_running else "NOT FOUND"})
    if not ctl_running:
        alive = False

    # 2. ansible-playbook process
    ans_out = await _jb_cmd("ps aux | grep ansible-playbook | grep -v grep")
    ans_running = "ansible-playbook" in ans_out
    ans_detail = "Running"
    if ans_running:
        import re
        pids = re.findall(r"^\S+\s+(\d+)\s+", ans_out, re.MULTILINE)
        if pids:
            ans_detail = f"Running (PIDs: {', '.join(pids[:3])})"
    else:
        ans_detail = "NOT FOUND"
        alive = False
    checks.append({"check": "ansible-playbook", "ok": ans_running, "detail": ans_detail})

    # 3. SSH session from jumpbox to target
    ssh_out = await _jb_cmd(f"ps aux | grep ssh | grep {short} | grep -v grep")
    ssh_active = bool(short and short in ssh_out)
    checks.append({"check": f"ssh-to-{short}", "ok": ssh_active,
                    "detail": "Active" if ssh_active else "No active SSH session (may be between tasks)"})

    # 4. Podman container (DCI agent runs inside a container on the jumpbox)
    pod_out = await _jb_cmd("ps aux | grep 'podman run' | grep dci-rhel-agent | grep -v grep")
    pod_running = "podman run" in pod_out
    checks.append({"check": "dci-agent-container", "ok": pod_running,
                    "detail": "Running" if pod_running else "NOT FOUND"})
    if not pod_running:
        alive = False

    return {
        "alive": alive,
        "checks": checks,
        "summary": "All processes alive" if alive else "PROCESS MISSING — workflow may have crashed",
    }


def _on_workflow_completed(correlation_id: str, result: dict):
    """Background poller callback: workflow finished (runs in poller thread)."""
    with _corr_lock:
        target = _corr_to_target.get(correlation_id)
    if not target:
        logger.warning("Completion callback: unknown correlation_id %s", correlation_id[:8])
        return

    success = result.get("return_code") == 0 and result.get("failure_count", 0) == 0
    status = "SUCCESS" if success else "FAILURE"
    logger.info("Background poller: %s completed — %s", target, status)

    from agents.local import fleet_state, workflow_events
    try:
        wf_start = 0.0
        for t, wf in _inflight_workflows.items():
            if wf.get("correlation_id") == correlation_id:
                wf_start = wf.get("start_time", 0.0)
                break
        elapsed = int(time.time() - wf_start) if wf_start else 0
        fleet_state.record_completion(target, success, elapsed)
    except Exception as e:
        logger.warning("Fleet state record failed: %s", e)

    try:
        event = {
            "type": "completed",
            "target_host": target,
            "correlation_id": correlation_id,
            "success": success,
            "return_code": result.get("return_code"),
            "failure_count": result.get("failure_count", 0),
        }
        if not success:
            event["failures"] = result.get("failures", [])[:3]
            event["error_lines"] = result.get("error_lines", [])[:5]
        workflow_events.push_event(event)
    except Exception as e:
        logger.warning("Workflow event push failed: %s", e)


def _on_heartbeat(correlation_id: str, heartbeat: dict):
    """Background poller callback: heartbeat received (runs in poller thread)."""
    with _corr_lock:
        target = _corr_to_target.get(correlation_id)
    if not target:
        return

    from agents.local import phase_expectations, workflow_events

    phase_str = heartbeat.get("phase", "")
    phase_num = phase_expectations.detect_phase_number(phase_str)
    if phase_num is None:
        return

    now = time.time()
    if target not in _phase_start_times:
        _phase_start_times[target] = {}
    if phase_num not in _phase_start_times[target]:
        _phase_start_times[target][phase_num] = now

    elapsed_minutes = (now - _phase_start_times[target][phase_num]) / 60.0

    if phase_expectations.is_phase_overdue(phase_num, elapsed_minutes, target):
        alert_key = (target, phase_num)
        if alert_key not in _stuck_alerted:
            _stuck_alerted.add(alert_key)
            timing = phase_expectations.get_phase_timing(phase_num, target)
            logger.warning("STUCK: %s phase %d at %.0fm (max: %dm)",
                           target, phase_num, elapsed_minutes, timing.get("max_minutes", 0))
            try:
                workflow_events.push_event({
                    "type": "stuck",
                    "target_host": target,
                    "correlation_id": correlation_id,
                    "phase": phase_num,
                    "phase_name": phase_str,
                    "elapsed_minutes": round(elapsed_minutes),
                    "max_minutes": timing.get("max_minutes", 0),
                })
            except Exception as e:
                logger.warning("Stuck event push failed: %s", e)


@asynccontextmanager
async def _lifespan(app):
    from agents import config
    problems = config.validate_mcp()
    if problems:
        for p in problems:
            logger.error("Config problem: %s", p)
        logger.warning("MCP server starting with %d config problem(s) — tools may fail", len(problems))

    from agents.local.knowledge_base import is_model_cached
    if not is_model_cached():
        logger.warning(
            "Embedding model not cached — first search_knowledge() call will download ~90MB. "
            "Run 'make download-model' to pre-download."
        )

    _load_inflight()
    # Rebuild correlation-to-target mapping from persisted inflight state
    with _corr_lock:
        for target, wf in _inflight_workflows.items():
            cid = wf.get("correlation_id")
            if cid:
                _corr_to_target[cid] = target
    bridge.register_completion_callback(_on_workflow_completed)
    bridge.register_heartbeat_callback(_on_heartbeat)
    bridge.start_completion_poller()
    logger.info("MCP server starting (session: %s)", bridge.get_session_id()[:8])
    try:
        yield {}
    finally:
        bridge.stop_completion_poller()
        bridge.close()
        logger.info("MCP server stopped, temp subscription cleaned up")


mcp = FastMCP("dci-relay", lifespan=_lifespan)

_pubsub_healthy: bool | None = None
_pubsub_last_check: float = 0.0
_HEALTH_CHECK_INTERVAL = 300
_health_lock = asyncio.Lock()


def _error_response(e: Exception, tool_name: str = "") -> str:
    result = {
        "success": False,
        "error": str(e),
        "traceback": traceback.format_exc(),
    }
    if tool_name:
        _auto_record_if_failed(result, tool_name)
    return json.dumps(result, indent=2)


def _result_response(result: dict, tool_name: str = "") -> str:
    """Serialize a tool result and auto-record relay failures."""
    if tool_name:
        _auto_record_if_failed(result, tool_name)
    return json.dumps(result, indent=2)


async def _preflight(tool_name: str = "") -> str | None:
    """Run pre-flight health check. Returns JSON error string if unhealthy, None if OK.

    Auto-records failures to the relay KB for trend analysis.
    """
    global _pubsub_healthy, _pubsub_last_check

    async with _health_lock:
        now = time.time()
        if _pubsub_healthy is True and (now - _pubsub_last_check) < _HEALTH_CHECK_INTERVAL:
            return None

        health = bridge.check_pubsub_health()
        _pubsub_healthy = health["healthy"]
        _pubsub_last_check = now

    if not _pubsub_healthy:
        logger.error("Preflight failed: %s", health["error"])
        relay_kb.record_relay_issue(
            error=health["error"],
            diagnosis="Preflight health check failed",
            resolution="",
            resolved=False,
            tool_name=tool_name,
            health_check=health.get("details", []),
        )
        return json.dumps({
            "success": False,
            "error": f"RELAY UNREACHABLE: {health['error']}",
            "_health_check": health.get("details", []),
        }, indent=2)

    # [AGENT-ADDED] Check actual pull health, not just credentials.
    # The poller can be dead (78 errors) while credentials are fine.
    diag = bridge.get_connection_diagnostics()
    pull_errors = diag.get("pull_error_count", 0)
    diagnosis = diag.get("diagnosis", "")
    if pull_errors >= 10 or diagnosis.startswith("PULL_STALE"):
        logger.warning("Preflight: pull health degraded (%d errors, %s), triggering recovery",
                        pull_errors, diagnosis)
        bridge.refresh_subscription()
        diag = bridge.get_connection_diagnostics()
        if diag.get("pull_error_count", 0) >= 10:
            return json.dumps({
                "success": False,
                "error": f"Pub/Sub poller unrecoverable: {diag['diagnosis']}",
                "_connection_state": diag,
            }, indent=2)

    return None


def _auto_record_if_failed(result: dict, tool_name: str) -> None:
    """If a tool result indicates a relay-level failure, record it to the relay KB."""
    if not isinstance(result, dict):
        return
    if result.get("success", True):
        return
    error = result.get("error", "")
    if not error:
        return
    relay_keywords = ["timeout", "relay", "pub/sub", "pubsub", "daemon", "ssh",
                       "jumpbox", "unreachable", "connection", "transport"]
    if not any(kw in error.lower() for kw in relay_keywords):
        return
    relay_kb.record_relay_issue(
        error=error,
        diagnosis="Auto-detected from tool result",
        resolution="",
        resolved=False,
        tool_name=tool_name,
        diagnostics=result.get("_diagnostics", []),
    )


def _reset_server_password_if_overridden(target_host: str) -> None:
    """After a successful deployment, remove per-server password override.

    A fresh OS deployment resets the password to the DCI default. If the
    server had a per-server override, remove it from run_config.yml so
    future SSH uses the default password.
    """
    import fcntl
    import re as _re
    import subprocess

    rc_path = Path(__file__).resolve().parent.parent / "run_config.yml"
    hostname = target_host.split(".")[0]

    with open(rc_path) as f:
        content = f.read()

    pattern = _re.compile(
        rf"(  {_re.escape(hostname)}:\n(?:    (?:fqdn)[^\n]*\n)*)"
        rf"    target_password: [^\n]+\n",
    )
    if not pattern.search(content):
        return

    logger.info("Removing per-server password override for %s (deployment succeeded)", hostname)
    new_content = pattern.sub(r"\1", content)

    with open(rc_path, "w") as f:
        f.write(new_content)

    repo_root = str(rc_path.parent)
    git_lock_path = Path(repo_root) / ".git" / "dci-agent.lock"
    lock_file = open(git_lock_path, "w")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        subprocess.run(["git", "add", "run_config.yml"], cwd=repo_root, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"Reset {hostname} password to default after successful deployment"],
            cwd=repo_root, capture_output=True,
        )
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=repo_root, capture_output=True)
    finally:
        lock_file.close()
    logger.info("Password override removed and pushed for %s", hostname)


async def _capture_server_profile(target_host: str, workflow_result: dict) -> None:
    """Capture server state after a workflow run and persist the profile."""
    from agents.local.knowledge_base import capture_server_state, save_server_profile

    try:
        diag_result = await bridge.send_command(
            "ssh.diagnostics",
            {"context_hint": "post-run profile", "target_host": target_host},
            timeout=60,
        )
    except Exception:
        logger.warning("Could not reach target %s for profile capture", target_host)
        return

    if not isinstance(diag_result, dict) or not diag_result.get("success", False):
        logger.warning("Diagnostics failed for profile capture on %s", target_host)
        return

    state = capture_server_state(diag_result.get("stdout", ""))

    run_success = False
    if isinstance(workflow_result, dict):
        run_success = workflow_result.get("success", False)

    save_server_profile(target_host, state, run_result={
        "success": run_success,
        "rhel_topic": workflow_result.get("_settings_sync", {}).get("topic", "") if isinstance(workflow_result, dict) else "",
    })
    logger.info("Server profile saved for %s", target_host)


def _resolve_target(target_host: str, topic: str = "") -> dict:
    """Resolve target_host, settings_file, and topic from run_config.yml."""
    import yaml
    rc_path = Path(__file__).resolve().parent.parent / "run_config.yml"
    with open(rc_path) as f:
        rc = yaml.safe_load(f) or {}

    if not target_host:
        target_host = rc.get("target", "")
    if not target_host:
        return {"success": False, "error": "No target_host provided and no default target in run_config.yml."}

    short = target_host.split(".")[0]
    servers = rc.get("servers", {})
    server_cfg = servers.get(short, {})
    if short in servers:
        target_host = server_cfg.get("fqdn", target_host)

    effective_topic = topic

    settings_file = f"/etc/dci-rhel-agent/settings_current_{short}.yml"
    return {
        "success": True,
        "target_host": target_host,
        "settings_file": settings_file,
        "topic": effective_topic,
    }


@mcp.tool(structured_output=False)
async def dci_workflow_run(
    verbosity: int = 0,
    settings_file: str = "",
    target_host: str = "",
    topic: str = "",
) -> str:
    """Trigger the full DCI Ansible workflow (OS deploy, SAP prep, benchmark, results).

    Returns immediately after the relay acknowledges the command (~3-5 seconds).
    Use dci_workflow_status() to poll for progress and the final result.
    Multiple workflows can run in parallel on different target servers.

    Args:
        verbosity: Ansible verbosity level. Range: 0-4. Default: 0.
                   0=quiet, 2=task detail, 3=connection detail, 4=full debug.
                   Example: 2
        settings_file: Absolute path on the jumpbox to the DCI settings file.
                       Format: /etc/dci-rhel-agent/settings_current_<hostname>.yml
                       Example: /etc/dci-rhel-agent/settings_current_target-1.yml
                       If empty, auto-generated from target_host and run_config.yml.
        target_host: FQDN of the target server. Required for parallel runs.
                     Format: <hostname>.<domain> (e.g. target-1.example.corp)
                     Do NOT use short hostname or IP address.
                     If empty, uses default target from run_config.yml.
        topic: RHEL topic. Format: RHEL-<major>.<minor> (e.g. RHEL-10.2, RHEL-9.8)
               Must be specified explicitly — there is no default.

    Returns:
        JSON with: success (bool), correlation_id (str), message (str),
        _settings_sync (dict), _diagnostics (list).

    Errors:
        - "No target_host provided": set target_host or default in run_config.yml
        - "already running on <host>": workflow in progress, stop it first
        - "missing disk_map": run /dci-configure --discover first
    """
    try:
        err = await _preflight("dci_workflow_run")
        if err:
            return err

        resolved = _resolve_target(target_host, topic)
        if not resolved["success"]:
            return json.dumps(resolved, indent=2)

        effective_target = resolved["target_host"]
        effective_topic = resolved.get("topic", "")

        # [AGENT-ADDED] Guard against duplicate starts for the same target
        async with _inflight_lock:
            if effective_target in _inflight_workflows:
                existing = _inflight_workflows[effective_target]
                elapsed = int(time.time() - existing["start_time"])
                return json.dumps({
                    "success": False,
                    "error": (
                        f"A workflow is already running for {effective_target} "
                        f"(started {elapsed}s ago, correlation: {existing['correlation_id'][:8]}). "
                        f"Use dci_workflow_status() to check progress, or "
                        f"dci_workflow_stop() to cancel it first."
                    ),
                }, indent=2)

        effective_settings = settings_file or resolved["settings_file"]

        payload = {"verbosity": verbosity}
        if effective_settings:
            payload["settings_file"] = effective_settings
        if effective_target:
            payload["target_host"] = effective_target

        # [AGENT-ADDED] Regenerate settings file if topic was specified, then embed in payload
        short = effective_target.split(".")[0] if effective_target else ""
        if short:
            settings_path = Path(__file__).resolve().parent.parent / "settings" / f"settings_current_{short}.yml"
            if effective_topic:
                import subprocess
                gen_result = subprocess.run(
                    ["python3", "-m", "tools.configure_target", "generate", short, effective_topic],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
                if gen_result.returncode != 0:
                    return json.dumps({
                        "success": False,
                        "error": f"Settings generation failed for {short} {effective_topic}: {gen_result.stderr[:500]}",
                    }, indent=2)
                logger.info("Regenerated settings for %s topic=%s", short, effective_topic)
            if settings_path.exists():
                payload["settings_content"] = settings_path.read_text()

        # [AGENT-ADDED] Sync hooks repo before dispatch (commit + push pending changes)
        try:
            from tools.sync_hooks import sync_hooks
            hooks_result = sync_hooks(commit_message=f"Auto-sync hooks before workflow run on {effective_target}")
            if not hooks_result["success"] and hooks_result.get("status") != "missing":
                return json.dumps({
                    "success": False,
                    "error": f"Hooks sync failed: {hooks_result['message']}",
                    "_hooks_sync": hooks_result,
                }, indent=2)
            logger.info("Hooks sync: %s", hooks_result["message"])
        except Exception as e:
            logger.warning("Hooks sync failed (non-fatal): %s", e)

        # [AGENT-ADDED] Non-blocking: publish command and wait for relay ACK only
        start_result = await bridge.send_command_start(
            "workflow.run", payload, ack_timeout=60,
        )

        if not start_result.get("ack_received"):
            return json.dumps({
                "success": False,
                "error": start_result.get("error", "No ACK from relay"),
                "_diagnostics": start_result.get("_diagnostics", []),
            }, indent=2)

        # [AGENT-ADDED] Store in-flight state for polling via dci_workflow_status
        correlation_id = start_result["correlation_id"]
        async with _inflight_lock:
            _inflight_workflows[effective_target] = {
                "correlation_id": correlation_id,
                "target_host": effective_target,
                "settings_file": effective_settings,
                "start_time": time.time(),
                "verbosity": verbosity,
                "last_heartbeat": None,
                "last_heartbeat_time": None,
                "status": "running",
                "result": None,
                "_settings_sync": {"topic": effective_topic, "settings_file": effective_settings},
            }
            _persist_inflight()
        with _corr_lock:
            _corr_to_target[correlation_id] = effective_target

        return json.dumps({
            "success": True,
            "started": True,
            "message": (
                f"Workflow started for {effective_target}. "
                f"Use dci_workflow_status(target_host=\"{effective_target}\") "
                f"to poll for progress and results."
            ),
            "target_host": effective_target,
            "correlation_id": correlation_id,
            "_diagnostics": start_result.get("_diagnostics", []),
        }, indent=2)

        # [AGENT-DISABLED] Post-workflow operations moved to dci_workflow_status()
        # try:
        #     await _capture_server_profile(effective_target, result)
        # except Exception as profile_err:
        #     logger.warning("Post-run profile capture failed (non-fatal): %s", profile_err)
        #
        # if isinstance(result, dict) and result.get("success"):
        #     try:
        #         _reset_server_password_if_overridden(effective_target)
        #     except Exception as pw_err:
        #         logger.warning("Password reset failed (non-fatal): %s", pw_err)

    except Exception as e:
        logger.exception("dci_workflow_run failed")
        return _error_response(e, "dci_workflow_run")


# [AGENT-ADDED] Non-blocking workflow status polling tool
@mcp.tool(structured_output=False)
async def dci_workflow_status(target_host: str = "") -> str:
    """Poll for the status and results of a running DCI workflow.

    Call after dci_workflow_run() to check progress. Returns heartbeat info
    while running, or the full result when the workflow completes.
    Poll every 60-90 seconds.

    Args:
        target_host: FQDN of the target server.
                     Format: <hostname>.<domain> (e.g. target-1.example.corp)
                     If empty and exactly one workflow is running, uses that one.

    Returns:
        JSON with: success (bool), status ("running"|"completed"|"failed"),
        target_host (str), elapsed_seconds (int), relay_info (dict with
        last_phase, last_output_line, last_heartbeat_age).
        On completion: full workflow result with success/failure details.
    """
    try:
        effective_target = target_host

        async with _inflight_lock:
            if not effective_target:
                if len(_inflight_workflows) == 1:
                    effective_target = next(iter(_inflight_workflows))
                elif len(_inflight_workflows) > 1:
                    targets = list(_inflight_workflows.keys())
                    return json.dumps({
                        "success": False,
                        "error": (
                            f"Multiple workflows in-flight: {targets}. "
                            f"Specify target_host to check a specific one."
                        ),
                        "inflight_targets": targets,
                    }, indent=2)
                else:
                    return json.dumps({
                        "success": False,
                        "error": "No workflows in-flight. Start one with dci_workflow_run().",
                    }, indent=2)

            wf = _inflight_workflows.get(effective_target)

        if not wf:
            try:
                relay_list = await bridge.send_command("workflow.list", {}, timeout=120)
                if isinstance(relay_list, dict) and relay_list.get("success"):
                    for rw in relay_list.get("workflows", []):
                        if rw.get("target_host") == effective_target:
                            elapsed = int(rw.get("running_seconds", 0))
                            result_data = {
                                "success": True,
                                "status": "running",
                                "message": (
                                    f"Workflow for {effective_target} is running on the relay "
                                    f"({elapsed}s elapsed) but has no local tracking state. "
                                    f"This can happen after an MCP server restart. "
                                    f"Use dci_workflow_list to monitor it."
                                ),
                                "relay_info": rw,
                            }

                            # [AGENT-ADDED] Auto-register so heartbeats and status polling work
                            corr_id = rw.get("correlation_id", "")
                            if corr_id:
                                async with _inflight_lock:
                                    _inflight_workflows[effective_target] = {
                                        "correlation_id": corr_id,
                                        "target_host": effective_target,
                                        "settings_file": rw.get("settings_file", ""),
                                        "start_time": time.time() - elapsed,
                                        "verbosity": 0,
                                        "last_heartbeat": {
                                            "phase": rw.get("last_phase", ""),
                                            "last_output_line": rw.get("last_output_line", ""),
                                        },
                                        "last_heartbeat_time": time.time(),
                                        "status": "running",
                                        "result": None,
                                    }
                                    _persist_inflight()
                                with _corr_lock:
                                    _corr_to_target[corr_id] = effective_target
                                logger.info("Auto-registered workflow for %s from relay (corr: %s)", effective_target, corr_id[:8])

                            hb_age = rw.get("last_heartbeat_age", 0)
                            if hb_age > 600:
                                alive = await _monitoring_checklist(effective_target)
                                result_data["process_check"] = alive

                            return json.dumps(result_data, indent=2)
            except Exception as e:
                logger.warning("Relay workflow.list fallback failed: %s", e)

            return json.dumps({
                "success": False,
                "error": f"No workflow found for {effective_target}.",
            }, indent=2)

        if wf["status"] in ("completed", "failed"):
            return json.dumps({
                "success": True,
                "status": wf["status"],
                "result": wf["result"],
                "elapsed_seconds": int(time.time() - wf["start_time"]),
            }, indent=2)

        correlation_id = wf["correlation_id"]
        poll_result = await bridge.check_for_result(correlation_id, poll_seconds=5)

        if poll_result["status"] == "completed":
            result = poll_result["result"]

            try:
                await _capture_server_profile(effective_target, result)
            except Exception as profile_err:
                logger.warning("Post-run profile capture failed (non-fatal): %s", profile_err)

            try:
                from agents.local.run_journal import log_workflow_completed
                log_workflow_completed(
                    run_id="",
                    target_host=effective_target,
                    rhel_topic=wf.get("_settings_sync", {}).get("topic", ""),
                    attempt_number=0,
                    success=result.get("success", False),
                    elapsed_seconds=int(time.time() - wf["start_time"]),
                    phase_reached=result.get("phase_reached", 0),
                    failing_task=result.get("failing_task", ""),
                    error_summary=(result.get("error_summary", "") or result.get("error", ""))[:500],
                )
            except Exception as journal_err:
                logger.warning("Journal record failed (non-fatal): %s", journal_err)

            try:
                from agents.local.knowledge_base import record_fix
                topic = wf.get("_settings_sync", {}).get("topic", "")
                success = result.get("success", False)
                error_summary = result.get("error_summary", "") or result.get("error", "")
                record_fix(
                    error_pattern=error_summary[:200] if not success else "workflow_success",
                    diagnosis=f"Workflow {'succeeded' if success else 'failed'} on {effective_target}",
                    fix_applied="",
                    files_changed=[],
                    success=success,
                    target_host=effective_target,
                    rhel_version=topic,
                    source="agent",
                )
            except Exception as kb_err:
                logger.warning("Knowledge base record failed (non-fatal): %s", kb_err)

            if isinstance(result, dict) and result.get("success"):
                try:
                    _reset_server_password_if_overridden(effective_target)
                except Exception as pw_err:
                    logger.warning("Password reset failed (non-fatal): %s", pw_err)

            wf["status"] = "completed" if result.get("success") else "failed"
            wf["result"] = result
            result["_settings_sync"] = wf.get("_settings_sync", {})
            result["workflow_run_number"] = wf.get("run_number", 0)

            _inflight_workflows.pop(effective_target, None)
            _persist_inflight()

            try:
                elapsed = int(time.time() - wf["start_time"])
                sync = wf.get("_settings_sync", {})
                phase_timings.record_run(
                    target_host=effective_target,
                    topic=sync.get("topic", ""),
                    total_seconds=elapsed,
                    success=bool(result.get("success")),
                    phases_reached=result.get("phase_reached", 5 if result.get("success") else 0),
                )
            except Exception as timing_err:
                logger.warning("Phase timing record failed (non-fatal): %s", timing_err)

            # Only do full cleanup on success — failed runs keep context
            # intact for the fix-retry loop (up to 5 attempts).
            if result.get("success"):
                try:
                    reset_info = bridge.reset_between_runs()
                    logger.info("Post-workflow cleanup: %s", reset_info.get("message", "done"))
                except Exception as sub_err:
                    logger.warning("Post-workflow cleanup failed (non-fatal): %s", sub_err)
            else:
                try:
                    bridge.drain_subscription()
                except Exception:
                    pass

            return _result_response(result, "dci_workflow_status")

        if poll_result["status"] == "running":
            hb = poll_result.get("last_heartbeat", {})
            wf["last_heartbeat"] = hb
            wf["last_heartbeat_time"] = time.time()

            elapsed = int(time.time() - wf["start_time"])
            result_data = {
                "success": True,
                "status": "running",
                "target_host": effective_target,
                "elapsed_seconds": elapsed,
                "heartbeat": hb,
            }

            hb_age = hb.get("elapsed_seconds", 0)
            if hb_age > 0:
                hb_age = elapsed - hb_age
            if hb_age > 600:
                alive = await _monitoring_checklist(effective_target)
                result_data["process_check"] = alive

            return json.dumps(result_data, indent=2)

        elapsed = int(time.time() - wf["start_time"])
        if wf["last_heartbeat_time"] and (time.time() - wf["last_heartbeat_time"]) > 120:
            wf["status"] = "lost"
            _inflight_workflows.pop(effective_target, None)
            _persist_inflight()
            return json.dumps({
                "success": False,
                "status": "lost",
                "error": (
                    f"Relay lost: no heartbeat for {effective_target} in over 120s. "
                    f"The relay may have crashed."
                ),
                "elapsed_seconds": elapsed,
                "last_heartbeat": wf.get("last_heartbeat"),
            }, indent=2)

        try:
            relay_list = await bridge.send_command("workflow.list", {}, timeout=120)
            if isinstance(relay_list, dict) and relay_list.get("success"):
                for rw in relay_list.get("workflows", []):
                    if rw.get("target_host") == effective_target:
                        return json.dumps({
                            "success": True,
                            "status": "running",
                            "target_host": effective_target,
                            "elapsed_seconds": elapsed,
                            "message": "Workflow confirmed running on relay (no new heartbeat in this poll cycle).",
                            "relay_info": rw,
                        }, indent=2)
        except Exception as e:
            logger.warning("Relay workflow.list confirmation failed: %s", e)

        return json.dumps({
            "success": True,
            "status": "running",
            "target_host": effective_target,
            "elapsed_seconds": elapsed,
            "message": "No new messages in this poll cycle. Workflow may still be running.",
        }, indent=2)

    except Exception as e:
        logger.exception("dci_workflow_status failed")
        return _error_response(e, "dci_workflow_status")


@mcp.tool(structured_output=False)
async def dci_ssh_execute(command: str, timeout: int = 120, target_host: str = "") -> str:
    """Run a read-only shell command on the DCI target server via SSH.

    Executed through a two-hop SSH chain: relay -> jumpbox -> target.
    Only allowlisted commands are permitted (cat, ls, grep, systemctl status,
    rpm -qa, df, etc.). Blocked: rm, mkfs, reboot, echo $PASSWORD, eval.

    Args:
        command: Shell command to execute on the target.
                 Must start with an allowlisted prefix.
                 Examples: "cat /etc/redhat-release", "systemctl status sshd",
                           "rpm -qa | grep sap", "df -h", "getenforce"
        timeout: Maximum seconds to wait. Range: 1-600. Default: 120.
        target_host: FQDN of the target server.
                     Format: <hostname>.<domain> (e.g. target-1.example.corp)
                     If empty, uses default target from run_config.yml.

    Returns:
        JSON with: success (bool), command (str), stdout (str), stderr (str),
        exit_code (int), _diagnostics (list).

    Errors:
        - "BLOCKED": command matched the destruction blocklist
        - "BLOCKED (not in allowlist)": command prefix not in SSH allowlist
        - "Timeout": command exceeded timeout seconds
    """
    try:
        err = await _preflight("dci_ssh_execute")
        if err:
            return err

        payload = {"command": command, "timeout": timeout}
        if target_host:
            payload["target_host"] = target_host
        result = await bridge.send_command(
            "ssh.execute", payload, timeout=min(timeout, 300)
        )
        return _result_response(result, "dci_ssh_execute")
    except Exception as e:
        logger.exception("dci_ssh_execute failed")
        return _error_response(e, "dci_ssh_execute")


@mcp.tool(structured_output=False)
async def dci_ssh_diagnostics(context_hint: str = "", target_host: str = "") -> str:
    """Run the built-in diagnostic suite on the DCI target server.

    Collects OS version, kernel, memory, disk, SELinux, tuned profile,
    SAP-related config in one call. Use after a workflow failure.

    Args:
        context_hint: Focus area for diagnostics. Free text.
                      Examples: "sap-preconfigure failed", "PBOffline segfault",
                                "HANA won't start", "disk full"
                      If empty, runs the full generic diagnostic suite.
        target_host: FQDN of the target server.
                     Format: <hostname>.<domain> (e.g. target-1.example.corp)
                     If empty, uses default from run_config.yml.

    Returns:
        JSON with: success (bool), stdout (str with diagnostic output),
        stderr (str), exit_code (int).
    """
    try:
        err = await _preflight("dci_ssh_diagnostics")
        if err:
            return err

        payload = {"context_hint": context_hint}
        if target_host:
            payload["target_host"] = target_host
        result = await bridge.send_command(
            "ssh.diagnostics", payload, timeout=120
        )
        return _result_response(result, "dci_ssh_diagnostics")
    except Exception as e:
        logger.exception("dci_ssh_diagnostics failed")
        return _error_response(e, "dci_ssh_diagnostics")


@mcp.tool(structured_output=False)
async def dci_workflow_list() -> str:
    """List all currently running DCI workflows.

    No arguments. Returns all active workflows with target host, settings file,
    elapsed time, correlation ID, current phase, and last output line.

    Returns:
        JSON with: success (bool), count (int), workflows (list of dicts),
        completed (list of recently finished), completed_count (int).
    """
    try:
        err = await _preflight("dci_workflow_list")
        if err:
            return err

        result = await bridge.send_command("workflow.list", {}, timeout=120)
        return _result_response(result, "dci_workflow_list")
    except Exception as e:
        logger.exception("dci_workflow_list failed")
        return _error_response(e, "dci_workflow_list")


@mcp.tool(structured_output=False)
async def dci_fleet_status() -> str:
    """Poll all running and recently completed DCI workflows in a single call.

    Returns a unified fleet dashboard with per-workflow phase info, heartbeat
    state, alerts, and recent completions. Merges relay state (what's alive)
    with Mac-side fleet state (nr goals, counters).

    Used internally by /dci-run for fleet monitoring. One call replaces N
    individual dci_workflow_status() calls.
    """
    try:
        err = await _preflight("dci_fleet_status")
        if err:
            return err

        relay_timed_out = False
        try:
            relay_result = await bridge.send_command("workflow.list", {}, timeout=120)
        except Exception as relay_err:
            if "Timeout" in str(relay_err) or "DEADLINE_EXCEEDED" in str(relay_err):
                relay_timed_out = True
                relay_result = {"success": False, "workflows": [], "completed": []}
            else:
                raise

        from agents.local import fleet_state
        goals = fleet_state.get_goals()
        fleet_state.update_poll_time()

        # Check background poller cache for completions
        async with _inflight_lock:
            corr_map = {
                wf["correlation_id"]: target
                for target, wf in _inflight_workflows.items()
                if wf.get("correlation_id")
            }
        bg_completions = bridge.pop_pending_completions(list(corr_map.keys()))

        workflows = relay_result.get("workflows", []) if relay_result.get("success") else []
        completions = relay_result.get("completed", []) if relay_result.get("success") else []

        enriched = []
        for wf in workflows:
            target = wf["target_host"]
            goal = goals.get(target, {})
            nr_target = goal.get("nr_target", 1)
            nr_completed = goal.get("nr_completed", 0)
            entry = {
                "target_host": target,
                "status": "running",
                "running_seconds": wf.get("running_seconds", 0),
                "phase": wf.get("last_phase", ""),
                "last_output_line": wf.get("last_output_line", ""),
                "heartbeat_age": wf.get("last_heartbeat_age", 0),
                "nr_progress": f"{nr_completed}/{nr_target}" if nr_target > 0 else "",
                "alert": None,
            }
            if wf.get("last_heartbeat_age", 0) > 120:
                entry["alert"] = f"No heartbeat for {wf['last_heartbeat_age']}s"
            enriched.append(entry)

        # If relay timed out, build running list from inflight state + heartbeat cache
        if relay_timed_out:
            async with _inflight_lock:
                for target, wf in _inflight_workflows.items():
                    cid = wf.get("correlation_id", "")
                    if cid in bg_completions:
                        continue
                    goal = goals.get(target, {})
                    nr_target = goal.get("nr_target", 1)
                    nr_completed = goal.get("nr_completed", 0)
                    elapsed = int(time.time() - wf.get("start_time", time.time()))
                    hb = bridge.get_latest_heartbeat(cid) or {}
                    entry = {
                        "target_host": target,
                        "status": "running",
                        "running_seconds": elapsed,
                        "phase": hb.get("phase", "unknown (relay timeout)"),
                        "last_output_line": hb.get("last_output_line", ""),
                        "heartbeat_age": int(time.time() - hb["timestamp"]) if "timestamp" in hb else elapsed,
                        "nr_progress": f"{nr_completed}/{nr_target}" if nr_target > 0 else "",
                        "alert": "Relay timed out — showing cached state",
                    }
                    enriched.append(entry)

        completed_enriched = []
        for comp in completions:
            target = comp["target_host"]
            goal = goals.get(target, {})
            nr_target = goal.get("nr_target", 1)
            nr_completed = goal.get("nr_completed", 0)
            completed_enriched.append({
                "target_host": target,
                "status": "success" if comp["success"] else "failure",
                "elapsed_seconds": comp.get("elapsed_seconds", 0),
                "error_summary": comp.get("error_summary", ""),
                "age_seconds": comp.get("age_seconds", 0),
                "nr_progress": f"{nr_completed}/{nr_target}" if nr_target > 0 else "",
            })

        # Add completions discovered by the background poller
        for cid, result in bg_completions.items():
            target = corr_map.get(cid, "unknown")
            goal = goals.get(target, {})
            nr_target = goal.get("nr_target", 1)
            nr_completed = goal.get("nr_completed", 0)
            success = result.get("success", False) and result.get("failure_count", 1) == 0
            completed_enriched.append({
                "target_host": target,
                "status": "success" if success else "failure",
                "elapsed_seconds": 0,
                "error_summary": str(result.get("failures", [])[:1]) if not success else "",
                "age_seconds": 0,
                "nr_progress": f"{nr_completed}/{nr_target}" if nr_target > 0 else "",
                "_source": "background_poller",
            })

        attention = [e["target_host"] for e in enriched if e.get("alert")]

        result = {
            "success": True,
            "active_count": len(enriched),
            "completed_count": len(completed_enriched),
            "workflows": enriched,
            "completed": completed_enriched,
            "attention_needed": attention,
        }
        if relay_timed_out:
            diag = bridge.get_connection_diagnostics()
            result["_relay_timeout"] = True
            result["_connection_state"] = diag
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("dci_fleet_status failed")
        return _error_response(e, "dci_fleet_status")


@mcp.tool(structured_output=False)
async def dci_workflow_stop(target_host: str) -> str:
    """Stop a specific running DCI workflow by target hostname.

    Kills the dci-rhel-agent-ctl process on the jumpbox for this target.

    Args:
        target_host: FQDN of the target server to stop. Required.
                     Format: <hostname>.<domain> (e.g. target-1.example.corp)

    Returns:
        JSON with: success (bool), message (str).
    """
    try:
        err = await _preflight("dci_workflow_stop")
        if err:
            return err

        result = await bridge.send_command(
            "workflow.stop", {"target_host": target_host}, timeout=120
        )
        async with _inflight_lock:
            _inflight_workflows.pop(target_host, None)
            _persist_inflight()
        try:
            bridge.reset_between_runs()
        except Exception:
            pass
        return _result_response(result, "dci_workflow_stop")
    except Exception as e:
        logger.exception("dci_workflow_stop failed")
        return _error_response(e, "dci_workflow_stop")


@mcp.tool(structured_output=False)
async def dci_workflow_stop_all() -> str:
    """Stop all running DCI workflows.

    Kills all dci-rhel-agent-ctl processes on the jumpbox. Use this when
    you want to abort everything and start fresh.
    """
    try:
        err = await _preflight("dci_workflow_stop_all")
        if err:
            return err

        result = await bridge.send_command("workflow.stop_all", {}, timeout=120)
        async with _inflight_lock:
            _inflight_workflows.clear()
            _persist_inflight()
        return _result_response(result, "dci_workflow_stop_all")
    except Exception as e:
        logger.exception("dci_workflow_stop_all failed")
        return _error_response(e, "dci_workflow_stop_all")


@mcp.tool(structured_output=False)
async def dci_check_events() -> str:
    """Check for workflow events detected by the background Pub/Sub poller.

    Returns completions (pass/fail) and stuck-phase alerts that the poller
    captured since the last check. Events are drained on read — each event
    is returned exactly once.

    Call this at the start of each monitoring poll. If events are present,
    act on them immediately:
    - completed+success: print result, check nr for re-dispatch
    - completed+failure: print failure, enter triage
    - stuck: investigate the stuck phase

    Returns:
        JSON with: events (list of event dicts), count (int).
    """
    from agents.local import workflow_events
    events = workflow_events.pop_events()
    return json.dumps({"events": events, "count": len(events)}, indent=2)


@mcp.tool(structured_output=False)
async def dci_jumpbox_execute(command: str, timeout: int = 30) -> str:
    """Run a read-only command on the jumpbox for process and log inspection.

    Use to check workflow processes, container logs, or system state.
    Only allowlisted commands permitted (ps, podman logs, cat, tail, grep, etc.).

    Args:
        command: Shell command to execute on the jumpbox.
                 Examples: "ps aux | grep dci", "podman logs dci-relay 2>&1 | tail -20",
                           "cat /var/log/messages | grep anaconda | tail -30",
                           "sudo ipmitool -I lanplus -H <bmc-ip> -U root -P <pass> power status"
        timeout: Maximum seconds to wait. Range: 1-300. Default: 30.

    Returns:
        JSON with: success (bool), command (str), stdout (str),
        stderr (str), exit_code (int).
    """
    try:
        err = await _preflight("dci_jumpbox_execute")
        if err:
            return err

        result = await bridge.send_command(
            "jumpbox.execute", {"command": command, "timeout": timeout}
        )
        return _result_response(result, "dci_jumpbox_execute")
    except Exception as e:
        logger.exception("dci_jumpbox_execute failed")
        return _error_response(e, "dci_jumpbox_execute")


@mcp.tool(structured_output=False)
async def dci_relay_update() -> str:
    """Pull latest code on the relay machine and restart the daemon.

    Runs git pull --ff-only, then restarts. Relay unavailable ~2-3 seconds.
    NEVER call while a workflow is running -- kills the SSH tunnel.

    No arguments. Returns: JSON with success (bool), message (str),
    git_output (str showing what was pulled).
    """
    try:
        err = await _preflight("dci_relay_update")
        if err:
            return err

        result = await bridge.send_command("relay.update", {}, timeout=60)
        return _result_response(result, "dci_relay_update")
    except Exception as e:
        logger.exception("dci_relay_update failed")
        return _error_response(e, "dci_relay_update")


@mcp.tool(structured_output=False)
async def dci_server_profile(target_host: str = "") -> str:
    """Capture and persist the current state of a target server.

    SSHes in, runs diagnostics, parses into structured profile (RHEL version,
    kernel, SELinux, tuned, memory), saves to server_profiles.json.

    Args:
        target_host: FQDN of the target server.
                     Format: <hostname>.<domain> (e.g. target-1.example.corp)
                     If empty, uses default from run_config.yml.

    Returns:
        JSON with: success (bool), host (str), profile (dict with
        rhel_version, kernel, selinux, tuned_profile, memory_gb, last_run).
    """
    try:
        err = await _preflight("dci_server_profile")
        if err:
            return err

        from agents.local.knowledge_base import capture_server_state, save_server_profile

        payload = {"context_hint": "server profile capture"}
        if target_host:
            payload["target_host"] = target_host

        result = await bridge.send_command("ssh.diagnostics", payload)

        if not isinstance(result, dict) or not result.get("success", False):
            return json.dumps({
                "success": False,
                "error": f"Diagnostics failed: {result.get('error', 'unknown') if isinstance(result, dict) else result}",
            }, indent=2)

        diag_output = result.get("stdout", "")
        state = capture_server_state(diag_output)

        effective_host = target_host
        if not effective_host:
            import yaml
            rc_path = Path(__file__).resolve().parent.parent / "run_config.yml"
            with open(rc_path) as f:
                rc = yaml.safe_load(f) or {}
            effective_host = rc.get("target", "")

        profile_result = save_server_profile(effective_host, state)
        return json.dumps(profile_result, indent=2)

    except Exception as e:
        logger.exception("dci_server_profile failed")
        return _error_response(e, "dci_server_profile")


@mcp.tool(structured_output=False)
async def dci_jumpbox_ping() -> str:
    """Check connectivity to the jumpbox via the relay.

    No arguments. Verifies relay is running and SSH tunnel to jumpbox works.
    Use before attempting longer operations.

    Returns:
        JSON with: success (bool), stdout (str with hostname and uptime),
        exit_code (int). Look for "RELAY_PING_OK" in stdout.
    """
    try:
        err = await _preflight("dci_jumpbox_ping")
        if err:
            return err

        result = await bridge.send_command("jumpbox.ping", {}, timeout=120)
        return _result_response(result, "dci_jumpbox_ping")
    except Exception as e:
        logger.exception("dci_jumpbox_ping failed")
        return _error_response(e, "dci_jumpbox_ping")


@mcp.tool(structured_output=False)
async def dci_relay_health() -> str:
    """Show relay infrastructure health: Pub/Sub connectivity and issue history.

    No arguments. Runs a live Pub/Sub health check and combines with
    relay knowledge base history. Use when MCP tools are failing.

    Returns:
        JSON with: pubsub_healthy (bool), pubsub_details (list),
        relay_kb_stats (dict with issue counts), relay_kb_summary (str).
    """
    health = bridge.check_pubsub_health()
    stats = relay_kb.get_relay_stats()
    summary = relay_kb.get_relay_summary()

    return json.dumps({
        "pubsub_healthy": health["healthy"],
        "pubsub_details": health.get("details", []),
        "pubsub_error": health.get("error", ""),
        "relay_kb_stats": stats,
        "relay_kb_summary": summary,
    }, indent=2)


@mcp.tool(structured_output=False)
async def dci_preflight_check() -> str:
    """Run pre-flight environment cleanup before a workflow run.

    No arguments. Call at the start of every /dci-run before any other tool.
    Refreshes stale Pub/Sub subscriptions, cleans up orphans, verifies
    Pub/Sub health, and pings the jumpbox.

    Returns:
        JSON with: ready (bool), subscription_refresh (str),
        pubsub_healthy (bool), pubsub_details (list),
        jumpbox_reachable (bool), jumpbox_response (dict).
        If ready=false, enter relay recovery mode.
    """
    results = {}

    try:
        reset_info = bridge.reset_between_runs()
        results["subscription_refresh"] = reset_info.get("message", "done")
    except Exception as e:
        results["subscription_refresh"] = f"FAILED: {e}"

    health = bridge.check_pubsub_health()
    results["pubsub_healthy"] = health["healthy"]
    results["pubsub_details"] = health.get("details", [])
    if health.get("error"):
        results["pubsub_error"] = health["error"]

    global _pubsub_healthy, _pubsub_last_check
    async with _health_lock:
        _pubsub_healthy = health["healthy"]
        _pubsub_last_check = time.time()

    if health["healthy"]:
        try:
            ping_result = await bridge.send_command("jumpbox.ping", {}, timeout=120)
            results["jumpbox_reachable"] = True
            results["jumpbox_response"] = ping_result
        except Exception as e:
            results["jumpbox_reachable"] = False
            results["jumpbox_error"] = str(e)
    else:
        results["jumpbox_reachable"] = False
        results["jumpbox_error"] = "Skipped: Pub/Sub unhealthy"

    results["ready"] = results.get("pubsub_healthy", False) and results.get("jumpbox_reachable", False)
    return json.dumps(results, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
