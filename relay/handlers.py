"""
Relay command handlers (slimmed down).

Only 3 operations remain:
- workflow.run: git pull + dci-rhel-agent-ctl
- ssh.execute: run a command on the target server
- ssh.diagnostics: run diagnostic suites on the target server

All file and git operations now happen locally on the Mac.
"""

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import threading
from pathlib import Path

from . import config
from .safety import (
    check_jumpbox_ssh_allowlist,
    check_target_ssh_allowlist,
    check_workflow_paths,
    wrap_remote_output,
)
from .ssh_manager import SSHManager

logger = logging.getLogger(__name__)

_workflow_run_count = 0

_active_workflows: dict[str, dict] = {}
_active_workflows_lock = threading.Lock()

_completed_workflows: dict[str, dict] = {}
_completed_workflows_lock = threading.Lock()
_COMPLETED_TTL = 3600  # 60 min

_workflow_list_cache: dict | None = None
_workflow_list_cache_time: float = 0
_WORKFLOW_LIST_CACHE_TTL = 5  # seconds

GITHUB_REMOTE_URL = config.GITHUB_REMOTE_URL
BANNED_REMOTE_PATTERNS = config._rc.get("banned_hosts", []) + config._rc.get("banned_paths", [])


def _ensure_safe_remote(ssh: SSHManager, repo_root: str) -> None:
    """Check that the jumpbox repo remote does NOT point to a banned host.

    If origin points to a banned host, replace it with the configured
    GitHub remote. This is a hard safety rule.
    """
    check_cmd = f"cd {shlex.quote(repo_root)} && git remote -v"
    result = ssh.exec_on_jumpbox(check_cmd, timeout=10)
    if not result["success"]:
        logger.warning("Could not check git remote: %s", result["stderr"][:200])
        return

    remote_output = result["stdout"]
    for banned in BANNED_REMOTE_PATTERNS:
        if banned in remote_output:
            logger.warning(
                "BANNED remote detected (%s) in jumpbox repo. Replacing with GitHub.",
                banned,
            )
            fix_cmd = (
                f"cd {shlex.quote(repo_root)} && "
                f"git remote set-url origin {shlex.quote(GITHUB_REMOTE_URL)}"
            )
            fix_result = ssh.exec_on_jumpbox(fix_cmd, timeout=10)
            if fix_result["success"]:
                logger.info("Remote replaced: origin -> %s", GITHUB_REMOTE_URL)
            else:
                logger.error("Failed to replace remote: %s", fix_result["stderr"][:200])
            return


def _clear_jumpbox_known_host(ssh: SSHManager, hostname: str) -> None:
    """Remove a target's stale host key from the jumpbox's known_hosts.

    The jumpbox also SSHes to target servers during DCI runs, so its
    known_hosts can have stale entries after a redeployment.
    """
    result = ssh.exec_on_jumpbox(f"ssh-keygen -R {shlex.quote(hostname)} 2>/dev/null || true", timeout=10)
    if result["success"]:
        logger.info("Cleared stale host key for %s on jumpbox", hostname)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

def _detect_phase(line: str) -> str:
    """Parse Ansible output to detect the current workflow phase."""
    clean = line.strip()
    if "PLAY [" in clean:
        start = clean.find("[") + 1
        end = clean.find("]", start)
        if end > start:
            return f"play:{clean[start:end][:60]}"
    if "PLAY RECAP" in clean:
        return "recap"
    if "TASK [" in clean:
        start = clean.find("[") + 1
        end = clean.find("]", start)
        if end > start:
            return f"task:{clean[start:end][:60]}"
    return ""



def handle_workflow(ssh: SSHManager, payload: dict) -> dict:
    """
    Pull latest changes on the jumpbox, then run dci-rhel-agent-ctl.
    This is the only long-running handler.
    """
    global _workflow_run_count

    max_total_runs = 50
    with _active_workflows_lock:
        _workflow_run_count += 1
        current_count = _workflow_run_count

    if current_count > max_total_runs:
        return {
            "success": False,
            "error": (
                f"Relay limit: {max_total_runs} workflow runs reached in this "
                f"daemon session. Restart the relay to reset."
            ),
        }

    settings = payload.get("settings_file", config.SETTINGS_FILE)
    hooks_dir = payload.get("hooks_dir", config.HOOKS_DIR)

    path_error = check_workflow_paths(hooks_dir, settings)
    if path_error:
        return {"success": False, "error": path_error}

    try:
        verbosity = max(0, min(4, int(payload.get("verbosity", 0))))
    except (ValueError, TypeError):
        verbosity = 0

    target_host = payload.get("target_host", config.TARGET_HOST)
    heartbeat = payload.get("_heartbeat_publisher")

    # [AGENT-ADDED] Reject if a workflow is already running for this target
    with _active_workflows_lock:
        if target_host in _active_workflows:
            existing = _active_workflows[target_host]
            elapsed = int(time.time() - existing["start_time"])
            return {
                "success": False,
                "error": (
                    f"A workflow is already running for {target_host} "
                    f"(started {elapsed}s ago on thread {existing['thread_name']}). "
                    f"Stop it first with dci_workflow_stop(target_host=\"{target_host}\")."
                ),
            }

    with _active_workflows_lock:
        _active_workflows[target_host] = {
            "target_host": target_host,
            "settings_file": settings,
            "correlation_id": payload.get("_correlation_id", ""),
            "start_time": time.time(),
            "thread_name": threading.current_thread().name,
            "last_phase": "",
            "last_output_line": "",
            "last_heartbeat_time": time.time(),
            "last_heartbeat_seq": 0,
        }

    result = None
    try:
        result = _run_workflow(ssh, target_host, settings, hooks_dir, verbosity, heartbeat, current_count, payload)
        return result
    finally:
        with _active_workflows_lock:
            info = _active_workflows.pop(target_host, None)
        start_time = info["start_time"] if info else time.time()
        success = result.get("success", False) if result else False
        error_summary = ""
        if result and not success:
            failures = result.get("failures", [])
            if failures:
                error_summary = str(failures[0])[:500]
        with _completed_workflows_lock:
            _completed_workflows[target_host] = {
                "target_host": target_host,
                "success": success,
                "return_code": result.get("return_code", -1) if result else -1,
                "elapsed_seconds": round(time.time() - start_time),
                "error_summary": error_summary,
                "completed_at": time.time(),
                "expires_at": time.time() + _COMPLETED_TTL,
            }


def _is_git_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("git@")


def _resolve_hooks_dir(ssh, hooks_dir: str) -> dict:
    """Resolve a hooks directory: clone from git URL or use local path.

    If hooks_dir is a git URL (https:// or git@), clone or pull it into
    the jumpbox repo root (e.g. /agentic-dci-workflow/dci-hooks/).
    If it's already a local path, return it unchanged.

    Returns dict with 'path' (resolved local path) and 'error' (str or None).
    """
    if not _is_git_url(hooks_dir):
        return {"path": hooks_dir, "error": None}

    clone_path = f"{config.REPO_ROOT}/dci-hooks"

    check = ssh.exec_on_jumpbox(f"test -d {shlex.quote(clone_path)}/.git && echo EXISTS", timeout=10)

    if "EXISTS" in check.get("stdout", ""):
        pull = ssh.exec_on_jumpbox(
            f"cd {shlex.quote(clone_path)} && git pull --ff-only",
            timeout=60,
        )
        if not pull["success"]:
            return {"path": "", "error": f"git pull failed for hooks repo: {pull.get('stderr', '')[:500]}"}
        logger.info("Updated hooks repo at %s from %s", clone_path, hooks_dir)
    else:
        clone = ssh.exec_on_jumpbox(
            f"git clone {shlex.quote(hooks_dir)} {shlex.quote(clone_path)}",
            timeout=120,
        )
        if not clone["success"]:
            return {"path": "", "error": f"git clone failed for hooks repo: {clone.get('stderr', '')[:500]}"}
        logger.info("Cloned hooks repo %s to %s", hooks_dir, clone_path)

    return {"path": clone_path, "error": None}


def _run_workflow(
    ssh: SSHManager,
    target_host: str,
    settings: str,
    hooks_dir: str,
    verbosity: int,
    heartbeat=None,
    run_number: int = 0,
    payload: dict | None = None,
) -> dict:
    """Inner workflow logic, separated so handle_workflow can wrap it with tracking."""
    if heartbeat:
        heartbeat.update(phase="pre-flight")

    ssh.clear_known_host(target_host)
    _clear_jumpbox_known_host(ssh, target_host)

    _ensure_safe_remote(ssh, config.REPO_ROOT)

    # Discard any local modifications on the jumpbox before pulling.
    # The jumpbox only consumes code — local edits are stale artifacts.
    reset_cmd = f"cd {shlex.quote(config.REPO_ROOT)} && git checkout . 2>/dev/null; git clean -ffd 2>/dev/null"
    ssh.exec_on_jumpbox(reset_cmd, timeout=30)

    pull_cmd = f"cd {shlex.quote(config.REPO_ROOT)} && git pull --ff-only"
    pull_result = ssh.exec_on_jumpbox(pull_cmd, timeout=60)
    if not pull_result["success"]:
        # [AGENT-DISABLED] logger.warning("git pull failed (continuing anyway): %s", pull_result["stderr"][:200])
        # [AGENT-ADDED] Fatal — never run with stale code
        logger.error("git pull FAILED — aborting workflow: %s", pull_result["stderr"][:200])
        return {
            "success": False,
            "error": (
                "git pull failed on jumpbox. Cannot proceed with stale code. "
                f"stderr: {pull_result['stderr'][:500]}"
            ),
            "phase": "pre-flight",
        }

    if _is_git_url(hooks_dir):
        if heartbeat:
            heartbeat.update(phase="hooks-clone")
        resolved = _resolve_hooks_dir(ssh, hooks_dir)
        if resolved["error"]:
            return {"success": False, "error": resolved["error"], "phase": "pre-flight"}
        hooks_dir = resolved["path"]

    # Reload after hooks pull — run_config.yml lives in the private hooks repo
    config.reload_run_config()

    if heartbeat:
        heartbeat.update(phase="settings-deploy")

    settings_basename = os.path.basename(settings)
    deploy_target = settings

    # [AGENT-ADDED] Deploy settings from Pub/Sub payload or fall back to existing
    settings_content = (payload or {}).get("settings_content", "")
    if settings_content:
        staging_path = f"/tmp/{settings_basename}"
        ssh.sftp_write(staging_path, settings_content)
        copy_cmd = f"sudo cp {shlex.quote(staging_path)} {shlex.quote(deploy_target)}"
        copy_result = ssh.exec_on_jumpbox(copy_cmd, timeout=10)
        if not copy_result["success"]:
            logger.error("Failed to deploy settings from payload: %s", copy_result["stderr"][:200])
            return {
                "success": False,
                "error": f"Failed to deploy settings: {copy_result['stderr'][:500]}",
                "phase": "pre-flight",
            }
        ssh.exec_on_jumpbox(f"rm -f {shlex.quote(staging_path)}", timeout=5)
        logger.info("Deployed settings from payload: %s -> %s (%d bytes)", settings_basename, deploy_target, len(settings_content))
    else:
        deploy_check_cmd = f"test -f {shlex.quote(deploy_target)} && echo DEPLOYED || echo MISSING"
        deploy_check = ssh.exec_on_jumpbox(deploy_check_cmd, timeout=10)
        if "DEPLOYED" in deploy_check.get("stdout", ""):
            logger.info("No settings in payload, using existing at %s", deploy_target)
        else:
            return {
                "success": False,
                "error": (
                    f"Settings file '{settings_basename}' not included in payload "
                    f"and not found at deploy target ({deploy_target}). "
                    "Re-run with settings_content or deploy manually."
                ),
                "phase": "pre-flight",
            }

    # [AGENT-ADDED] Deploy inventory with ansible_python_interpreter for RHEL 10+.
    # The dci-inventory file lives in the hooks repo (private git).
    # The jumpbox already pulled the hooks repo, so copy from there.
    inv_src = os.path.join(hooks_dir, "dci-inventory")
    inv_check = ssh.exec_on_jumpbox(
        f"test -f {shlex.quote(inv_src)} && echo EXISTS || echo MISSING",
        timeout=10,
    )
    if "EXISTS" in inv_check.get("stdout", ""):
        inv_copy = ssh.exec_on_jumpbox(
            f"sudo cp {shlex.quote(inv_src)} /etc/dci-rhel-agent/inventory",
            timeout=10,
        )
        if inv_copy.get("success"):
            logger.info("Deployed DCI inventory from hooks repo (python3 interpreter)")
        else:
            logger.warning("Failed to deploy inventory: %s", inv_copy.get("stderr", "")[:200])

    if heartbeat:
        heartbeat.update(phase="ansible")

    cmd = (
        f"sudo dci-rhel-agent-ctl "
        f"--config {shlex.quote(settings)} --start --hooks {shlex.quote(hooks_dir)}"
    )

    _line_seq = [0]

    _FAILURE_MARKERS = ("fatal:", "FAILED!", "UNREACHABLE!")

    def _on_line(line):
        logger.info("[workflow] %s", line)
        _line_seq[0] += 1
        phase = _detect_phase(line)
        with _active_workflows_lock:
            entry = _active_workflows.get(target_host)
            if entry and _line_seq[0] > entry.get("last_heartbeat_seq", 0):
                if phase:
                    entry["last_phase"] = phase
                entry["last_output_line"] = line[-200:]
                entry["last_heartbeat_time"] = time.time()
                entry["last_heartbeat_seq"] = _line_seq[0]
        if heartbeat:
            heartbeat.update(line=line, phase=phase or "")
            if any(marker in line for marker in _FAILURE_MARKERS):
                heartbeat.send_now()

    logger.info("Running DCI workflow (attempt %d, verbosity %d)", run_number, verbosity)
    result = ssh.exec_on_jumpbox_streaming(
        cmd,
        timeout=config.WORKFLOW_TIMEOUT,
        get_pty=True,
        line_callback=_on_line,
    )

    stdout = result["stdout"]
    stderr = result["stderr"]

    recap = _parse_play_recap(stdout)
    failures = _extract_failures(stdout, stderr)

    if result["exit_code"] == 0 and recap["all_ok"]:
        success = True
        failures = []
    else:
        success = result["exit_code"] == 0 and len(failures) == 0

    if heartbeat:
        heartbeat.update(phase="completed")

    stderr_limit = {0: 4000, 1: 10000, 2: 20000}.get(verbosity, 50000)

    # [AGENT-ADDED] Extract full fatal/unreachable error blocks from stdout
    error_lines = []
    if not success and stdout:
        for line in stdout.splitlines():
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip('\r').strip()
            if "fatal:" in clean or "UNREACHABLE!" in clean or "FAILED!" in clean:
                error_lines.append(clean[:500])
            elif error_lines and clean and not clean.startswith("TASK [") and not clean.startswith("PLAY"):
                error_lines[-1] = error_lines[-1] + " " + clean[:200]

    return {
        "success": success,
        "return_code": result["exit_code"],
        "failure_count": len(failures),
        "failures": failures[:5],
        "play_recap": recap["hosts"] if recap["hosts"] else None,
        "error_lines": error_lines[:10] if error_lines else [],
        "raw_stdout_tail": wrap_remote_output(stdout[-4000:]) if stdout else "",
        "raw_stderr_tail": wrap_remote_output(stderr[-stderr_limit:]) if stderr else "",
        "workflow_run_number": run_number,
    }


def _parse_play_recap(stdout: str) -> dict:
    """Parse PLAY RECAP lines to determine per-host pass/fail status."""
    hosts = {}
    all_ok = False
    recap_section = False
    for line in stdout.splitlines():
        stripped = line.strip()
        # Strip ANSI escape codes and stray carriage returns (PTY output has \r\r\n)
        clean = re.sub(r'\x1b\[[0-9;]*m', '', stripped).strip('\r')
        if "PLAY RECAP" in clean:
            recap_section = True
            continue
        if recap_section and clean:
            # Format: hostname : ok=N changed=N unreachable=N failed=N ...
            m = re.match(
                r'(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)',
                clean,
            )
            if m:
                hosts[m.group(1)] = {
                    "ok": int(m.group(2)),
                    "changed": int(m.group(3)),
                    "unreachable": int(m.group(4)),
                    "failed": int(m.group(5)),
                }
            elif not clean.startswith(("ok=", "changed=", "unreachable=")):
                recap_section = False
    if hosts:
        all_ok = all(h["failed"] == 0 and h["unreachable"] == 0 for h in hosts.values())
    return {"hosts": hosts, "all_ok": all_ok}


def _extract_failures(stdout: str, stderr: str) -> list[dict]:
    stripped = stdout.lstrip()
    if stripped and stripped[0] in ('{', '['):
        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                return _parse_json_failures(data)
        except (json.JSONDecodeError, ValueError):
            pass
    return _parse_text_failures(stdout, stderr)


def _parse_json_failures(data: dict) -> list[dict]:
    failures = []
    for play in data.get("plays", []):
        for task in play.get("tasks", []):
            task_name = task.get("task", {}).get("name", "unknown")
            task_path = task.get("task", {}).get("path", "unknown")
            for host, result in task.get("hosts", {}).items():
                if result.get("failed") or result.get("unreachable"):
                    failures.append({
                        "task_name": task_name,
                        "task_file": task_path,
                        "host": host,
                        "module": task.get("task", {}).get("module", "unknown"),
                        "error_message": result.get("msg", result.get("stderr", ""))[:1000],
                    })
    return failures


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
_MSG_RE = re.compile(r'msg[=:]\s*["\']?(?P<msg>[^"\'}\n]+)')


def _parse_text_failures(stdout: str, stderr: str) -> list[dict]:
    failures = []
    current_task = "unknown"
    combined = stdout + "\n" + stderr
    has_failure_marker = False

    for line in combined.splitlines():
        clean = _ANSI_RE.sub('', line).strip('\r').strip()
        if not clean:
            continue

        if clean.startswith("TASK ["):
            bracket_end = clean.find("]")
            if bracket_end > 6:
                current_task = clean[6:bracket_end]
            continue

        if "fatal:" in clean or "FAILED!" in clean or "UNREACHABLE!" in clean:
            has_failure_marker = True
            is_unreachable = "UNREACHABLE!" in clean
            msg_match = _MSG_RE.search(clean)
            msg = msg_match.group("msg") if msg_match else "see raw output"
            failures.append({
                "task_name": current_task,
                "task_file": "unknown (text parse)",
                "host": "unknown",
                "module": "unknown",
                "error_message": msg[:1000],
                "failure_type": "unreachable" if is_unreachable else "failed",
            })
            if len(failures) >= 10:
                break

    if not failures and has_failure_marker:
        failures.append({
            "task_name": "unknown",
            "task_file": "unknown",
            "host": "unknown",
            "module": "unknown",
            "error_message": "Workflow failed. Could not parse specific task. Check raw output.",
        })
    return failures


# ---------------------------------------------------------------------------
# Workflow control (list / stop)
# ---------------------------------------------------------------------------

def handle_workflow_list(ssh: SSHManager, payload: dict) -> dict:
    """Return all running and recently completed workflows with heartbeat state."""
    global _workflow_list_cache, _workflow_list_cache_time

    now = time.time()
    if _workflow_list_cache and (now - _workflow_list_cache_time) < _WORKFLOW_LIST_CACHE_TTL:
        return _workflow_list_cache

    with _active_workflows_lock:
        workflows = []
        for target, info in _active_workflows.items():
            workflows.append({
                "target_host": info["target_host"],
                "settings_file": info["settings_file"],
                "correlation_id": info["correlation_id"],
                "running_seconds": round(now - info["start_time"]),
                "thread_name": info["thread_name"],
                "last_phase": info.get("last_phase", ""),
                "last_output_line": info.get("last_output_line", ""),
                "last_heartbeat_age": round(now - info.get("last_heartbeat_time", now)),
            })

    with _completed_workflows_lock:
        completions = []
        for target, info in _completed_workflows.items():
            if now < info["expires_at"]:
                completions.append({
                    "target_host": info["target_host"],
                    "success": info["success"],
                    "return_code": info["return_code"],
                    "elapsed_seconds": info["elapsed_seconds"],
                    "error_summary": info.get("error_summary", ""),
                    "completed_at": info["completed_at"],
                    "age_seconds": round(now - info["completed_at"]),
                })

    from . import daemon as _daemon
    result = {
        "success": True,
        "count": len(workflows),
        "workflows": workflows,
        "completed": completions,
        "completed_count": len(completions),
        "relay_git_sha": _daemon._relay_git_sha,
        "relay_uptime_seconds": round(now - _daemon._relay_start_time) if _daemon._relay_start_time else None,
    }
    _workflow_list_cache = result
    _workflow_list_cache_time = now
    return result


def handle_workflow_stop(ssh: SSHManager, payload: dict) -> dict:
    """Stop a specific running workflow by killing its process on the jumpbox."""
    target_host = payload.get("target_host", "")
    if not target_host:
        return {"success": False, "error": "target_host is required"}

    with _active_workflows_lock:
        info = _active_workflows.get(target_host)
        active_targets = list(_active_workflows.keys())
    if info is None:
        return {
            "success": False,
            "error": f"No running workflow for target {target_host}",
            "active_targets": active_targets,
        }

    settings_basename = os.path.basename(info["settings_file"])
    kill_cmd = f"sudo pkill -f {shlex.quote(settings_basename)} || true"
    result = ssh.exec_on_jumpbox(kill_cmd, timeout=15)
    logger.info("Stopped workflow for %s: %s", target_host, result["stdout"][:200])

    return {
        "success": True,
        "message": f"Sent kill signal to workflow for {target_host}",
        "target_host": target_host,
        "settings_file": info["settings_file"],
        "kill_output": result["stdout"][:500],
    }


def handle_workflow_stop_all(ssh: SSHManager, payload: dict) -> dict:
    """Stop all running workflows by killing all dci-rhel-agent-ctl processes."""
    with _active_workflows_lock:
        targets = list(_active_workflows.keys())

    if not targets:
        return {"success": True, "message": "No workflows running", "stopped": []}

    kill_cmd = "sudo pkill -f dci-rhel-agent-ctl || true"
    result = ssh.exec_on_jumpbox(kill_cmd, timeout=15)
    logger.info("Stopped all workflows: %s", result["stdout"][:200])

    return {
        "success": True,
        "message": f"Sent kill signal to {len(targets)} workflow(s)",
        "stopped": targets,
        "kill_output": result["stdout"][:500],
    }


# ---------------------------------------------------------------------------
# SSH to target server
# ---------------------------------------------------------------------------

def handle_ssh(ssh: SSHManager, payload: dict) -> dict:
    """Execute a command on the target server (via jumpbox two-hop)."""
    command = payload.get("command", "")
    target_host = payload.get("target_host", config.TARGET_HOST)
    try:
        timeout = max(1, min(300, int(payload.get("timeout", 120))))
    except (ValueError, TypeError):
        timeout = 120

    error = check_target_ssh_allowlist(command)
    if error:
        return {"success": False, "error": error, "command": command}

    _clear_jumpbox_known_host(ssh, target_host)

    result = ssh.exec_on_target(command, target_host=target_host, timeout=timeout)
    result["stdout"] = wrap_remote_output(result["stdout"])
    result["stderr"] = wrap_remote_output(result["stderr"])
    return result


def _prepare_jumpbox_ssh(ssh: SSHManager, command: str) -> str:
    """Auto-wrap jumpbox SSH commands with sshpass and host key handling.

    When the command is 'ssh root@<target> ...', this:
    1. Clears stale host keys on the jumpbox for the target
    2. Wraps with sshpass using the configured target password
    3. Adds StrictHostKeyChecking=no (target keys change every deploy)
    """
    from relay.safety import _extract_ssh_target
    stripped = command.strip()
    if not stripped.startswith(("ssh ", "sshpass ")):
        return command

    target = _extract_ssh_target(stripped)
    if not target:
        return command

    _clear_jumpbox_known_host(ssh, target)

    if stripped.startswith("sshpass "):
        return command

    password = config.get_target_password(target)
    if not password:
        return command

    return (
        f"sshpass -p {shlex.quote(password)} "
        f"ssh -o StrictHostKeyChecking=no {stripped[4:]}"
    )


def handle_jumpbox_execute(ssh: SSHManager, payload: dict) -> dict:
    """Execute a command on the jumpbox for process/log inspection."""
    command = payload.get("command", "")
    try:
        timeout = max(1, min(300, int(payload.get("timeout", 30))))
    except (ValueError, TypeError):
        timeout = 30

    error = check_jumpbox_ssh_allowlist(command)
    if error:
        return {"success": False, "error": error, "command": command}

    command = _prepare_jumpbox_ssh(ssh, command)

    result = ssh.exec_on_jumpbox(command, timeout=timeout)
    result["stdout"] = wrap_remote_output(result["stdout"])
    result["stderr"] = wrap_remote_output(result["stderr"])
    return result


def handle_diagnostics(ssh: SSHManager, payload: dict) -> dict:
    """Run a standard set of diagnostic commands on the target server."""
    context_hint = payload.get("context_hint", "")
    target_host = payload.get("target_host", config.TARGET_HOST)

    base_commands = [
        "uname -a",
        "cat /etc/redhat-release",
        "df -h",
        "free -h",
        "systemctl is-system-running",
        "getenforce",
        "journalctl -p err --no-pager --since '1 hour ago' | tail -50",
    ]

    extra = {
        "deployment": [
            "subscription-manager status 2>/dev/null || echo 'not registered'",
            "subscription-manager release 2>/dev/null || true",
            "yum repolist 2>/dev/null || dnf repolist 2>/dev/null || true",
            "ip addr show | grep 'inet ' | grep -v 127.0.0.1",
            "cat /etc/resolv.conf",
        ],
        "sap_prepare": [
            "cat /etc/redhat-release",
            "ansible --version 2>&1 | head -5",
            "rpm -qa | grep -i sap | sort",
            "tuned-adm active 2>/dev/null || true",
            "tuned-adm verify 2>/dev/null || true",
            "getenforce",
            "ausearch -m avc --start recent 2>/dev/null | tail -20 || true",
        ],
        "benchmark": [
            "systemctl status sapinit 2>/dev/null || echo 'sapinit not found'",
            "ls -la /hana/data /hana/log /hana/shared 2>/dev/null || echo 'HANA dirs missing'",
            "ls -la /archive/benchmarks/pbo/ 2>/dev/null || echo 'PBO archive missing'",
            "df -h /hana /archive 2>/dev/null || true",
        ],
        "hana": [
            "systemctl status sapinit 2>/dev/null || echo 'sapinit not found'",
            "ls -la /hana/data /hana/log /hana/shared 2>/dev/null || echo 'HANA dirs missing'",
            "tail -100 /var/tmp/hdbinst.log 2>/dev/null || echo 'No HANA install log'",
        ],
        "storage": ["lsblk", "pvs", "vgs", "lvs", "mount | grep -E 'hana|archive|dude'"],
        "network": ["ip addr show", "ss -tlnp", "cat /etc/resolv.conf"],
        "satellite": [
            "subscription-manager status 2>/dev/null || true",
            "subscription-manager list --installed 2>/dev/null || true",
            "yum repolist 2>/dev/null || true",
        ],
        "tuned": ["tuned-adm active 2>/dev/null || true", "tuned-adm verify 2>/dev/null || true"],
        "selinux": ["getenforce", "ausearch -m avc --start recent 2>/dev/null | tail -30 || true"],
    }

    commands = list(base_commands)
    hint = context_hint.lower()
    for keyword, cmds in extra.items():
        if keyword in hint:
            commands.extend(cmds)

    results = []
    for cmd in commands:
        r = ssh.exec_on_target(cmd, target_host=target_host, timeout=30)
        results.append({
            "command": cmd,
            "exit_code": r["exit_code"],
            "stdout": wrap_remote_output(r["stdout"][:800]),
        })

    return {"context_hint": context_hint, "diagnostic_count": len(results), "results": results}


# ---------------------------------------------------------------------------
# Jumpbox connectivity check
# ---------------------------------------------------------------------------

def handle_jumpbox_ping(ssh: SSHManager, payload: dict) -> dict:
    """Quick connectivity test: runs hostname on the jumpbox only.

    Includes relay git SHA and uptime so the caller can verify
    which code version is running without reading container logs.
    """
    from . import daemon as _daemon
    result = ssh.exec_on_jumpbox("hostname && uptime && echo RELAY_PING_OK", timeout=15)
    return {
        "success": result["success"],
        "stdout": wrap_remote_output(result["stdout"]),
        "exit_code": result["exit_code"],
        "relay_git_sha": _daemon._relay_git_sha,
        "relay_uptime_seconds": round(time.time() - _daemon._relay_start_time) if _daemon._relay_start_time else None,
    }


# ---------------------------------------------------------------------------
# Relay self-update
# ---------------------------------------------------------------------------

def _restart_daemon(repo_root: str = ""):
    """Restart the relay daemon.

    In a container: exit cleanly and let the restart policy bring us back.
    On bare metal: replace the current process via os.execv (same PID).
    """
    if os.environ.get("DCI_CONTAINERIZED"):
        logger.info("Container mode: exiting for restart (restart policy will recover)...")
        os._exit(0)
    if repo_root:
        os.chdir(repo_root)
    logger.info("Restarting relay daemon via os.execv from %s...", os.getcwd())
    os.execv(sys.executable, [sys.executable, "-m", "relay.daemon"])


def handle_relay_update(ssh: SSHManager, payload: dict) -> dict:
    """Pull latest code and restart the relay daemon.

    Always does git pull (container or bare metal). In container mode
    the repo is bind-mounted from the host, so git pull updates the
    host's working copy directly.
    """
    repo_root = str(Path(__file__).resolve().parent.parent)
    containerized = bool(os.environ.get("DCI_CONTAINERIZED"))

    git_dir = os.path.join(repo_root, ".git")
    if not os.path.exists(git_dir):
        return {
            "success": False,
            "error": f"Not a git repository: {repo_root} (no .git found)",
            "repo_root": repo_root,
            "cwd": os.getcwd(),
            "file": str(Path(__file__).resolve()),
        }

    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )

    git_output = ""
    if result.returncode != 0:
        git_output = f"git pull failed (non-fatal): {result.stderr[:300]}"
        logger.warning("git pull failed during relay update: %s", result.stderr[:300])
    else:
        git_output = result.stdout[:500]

    threading.Timer(2.0, _restart_daemon, args=[repo_root]).start()

    mode = "container (pull + restart)" if containerized else "bare metal (pull + restart)"
    return {
        "success": True,
        "message": f"Relay will restart in ~2 seconds. Mode: {mode}",
        "git_output": git_output,
        "repo_root": repo_root,
    }


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

HANDLERS: dict[str, callable] = {
    "workflow.run": handle_workflow,
    "workflow.list": handle_workflow_list,
    "workflow.stop": handle_workflow_stop,
    "workflow.stop_all": handle_workflow_stop_all,
    "ssh.execute": handle_ssh,
    "ssh.diagnostics": handle_diagnostics,
    "jumpbox.ping": handle_jumpbox_ping,
    "jumpbox.execute": handle_jumpbox_execute,
    "relay.update": handle_relay_update,
}


def reset_session():
    """Reset session state (called when a new session starts)."""
    global _workflow_run_count
    _workflow_run_count = 0
    with _active_workflows_lock:
        _active_workflows.clear()
