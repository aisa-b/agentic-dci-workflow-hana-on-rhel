"""
Agent hooks -- Mac-side soft gates for safety enforcement.

These hooks run BEFORE tool calls leave the Mac (PreToolUse) and AFTER
results come back (PostToolUse). They provide fast feedback to the agent
and an audit trail. The relay's safety module is the hard gate -- these
hooks are a first line of defense that lets the agent self-correct
without a round-trip to the jumpbox.
"""

import json
import logging
import re
import datetime

from . import config

logger = logging.getLogger(__name__)

_commit_shas: list[str] = []
_workflow_run_count = 0
_audit_log_path = config.LOG_DIR / "agent_audit.jsonl"


def _audit(entry: dict):
    """Append a JSON entry to the local audit log."""
    entry["timestamp"] = datetime.datetime.now().isoformat()
    try:
        from .local.filelock import locked_append
        locked_append(_audit_log_path, json.dumps(entry, default=str))
    except ImportError:
        _audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_audit_log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    try:
        from .local.events import emit as unified_emit
        unified_emit(f"audit.{entry.get('event', 'unknown')}", data=entry)
    except Exception as e:
        logger.warning("Unified event forward failed in _audit: %s", e)


# ---------------------------------------------------------------------------
# Allowlist / blocklist (mirrors relay/safety.py for fast local checking)
# ---------------------------------------------------------------------------

_SSH_BLOCKLIST = [
    "rm ", "rm\t", " rm ", ";rm ", "|rm ",
    "rmdir ", "unlink ", "shred ",
    "mkfs", "dd if=", "fdisk", "parted ", "wipefs",
    "reboot", "shutdown", "poweroff", "halt",
    "init 0", "init 6",
    "userdel", "groupdel",
    "iptables -F", "iptables -X",
    "| rm", "xargs rm",
    "git clean", "git reset --hard", "git push --force", "git push -f",
    "git rm", "git push origin --delete",
    "gh repo delete", "gh pr close",
]

# Load banned patterns from run_config.yml
try:
    from config_loader import load_run_config
    _rc = load_run_config()
    _SSH_BLOCKLIST.extend(_rc.get("banned_hosts", []))
    _SSH_BLOCKLIST.extend(_rc.get("banned_paths", []))
except Exception:
    pass


def _check_ssh_blocklist(command: str) -> str | None:
    """Returns an error message if the command is blocked, None if OK."""
    cmd_lower = command.lower()
    for pattern in _SSH_BLOCKLIST:
        if pattern.lower() in cmd_lower:
            return (
                f"BLOCKED: Command contains destructive pattern '{pattern.strip()}'. "
                "Use read-only diagnostics or reversible operations only."
            )
    return None


# ---------------------------------------------------------------------------
# Credential leak detection -- scrub secrets before they reach logs or Claude
# ---------------------------------------------------------------------------

_CREDENTIAL_PATTERNS = [
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),                  # GitHub PAT (classic)
    re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),           # GitHub PAT (fine-grained)
    re.compile(r'"private_key"\s*:\s*"-----BEGIN'),         # GCP service account key
    re.compile(r'"private_key_id"\s*:\s*"[a-f0-9]{40}"'),  # GCP SA key ID
    re.compile(r"AIza[A-Za-z0-9_-]{35}"),                  # Google API key
    re.compile(r"ya29\.[A-Za-z0-9_-]+"),                   # Google OAuth token
    re.compile(r"gho_[A-Za-z0-9]{36,}"),                   # GitHub OAuth token
    re.compile(r"(?i)(password|passwd|secret_key|api_key|access_key)\s*[:=]\s*['\"]?[A-Za-z0-9+/=]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
]

CREDENTIAL_REDACTION = "[CREDENTIAL-REDACTED-BY-AGENT]"


def scrub_credentials(text: str) -> str:
    """Remove credential patterns from text before logging or sending to Claude."""
    result = text
    for pattern in _CREDENTIAL_PATTERNS:
        result = pattern.sub(CREDENTIAL_REDACTION, result)
    return result


def contains_credentials(text: str) -> bool:
    """Check if text contains any credential patterns."""
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _validate_no_delete(original: str, replacement: str) -> str | None:
    """
    Check that no substantive lines are deleted in an edit.
    Returns an error message if lines would be deleted, None if OK.
    """
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped not in replacement:
            commented = f"# [AGENT-DISABLED] {stripped}"
            alt = f"#{line}"
            if commented not in replacement and alt not in replacement:
                if not any(stripped in rline for rline in replacement.splitlines()):
                    return (
                        f"BLOCKED: Line would be deleted: '{stripped[:80]}'. "
                        "You MUST comment it out (prefix with '# [AGENT-DISABLED] '), not remove it."
                    )
    return None


# ---------------------------------------------------------------------------
# Pre-tool-use hooks
# ---------------------------------------------------------------------------

def pre_tool_use(tool_name: str, tool_input: dict) -> str | None:
    """
    Called before a tool is executed. Returns an error string to block
    the call, or None to allow it.
    """
    _audit({"event": "pre_tool_use", "tool": tool_name, "input_keys": list(tool_input.keys())})

    if tool_name == "ssh_execute":
        command = tool_input.get("command", "")
        return _check_ssh_blocklist(command)

    if tool_name == "edit_remote_file":
        original = tool_input.get("original", "")
        replacement = tool_input.get("replacement", "")
        if original and replacement:
            return _validate_no_delete(original, replacement)

    return None


# ---------------------------------------------------------------------------
# Post-tool-use hooks
# ---------------------------------------------------------------------------

def post_tool_use(tool_name: str, tool_input: dict, result: dict) -> dict:
    """
    Called after a tool returns its result. Scrubs credentials from output,
    tracks commits, and logs.

    Returns the (possibly scrubbed) result dict.
    """
    global _workflow_run_count

    result_str = json.dumps(result, default=str)
    if contains_credentials(result_str):
        logger.warning(
            "CREDENTIAL DETECTED in %s output -- scrubbing before it reaches Claude",
            tool_name,
        )
        scrubbed_str = scrub_credentials(result_str)
        result = json.loads(scrubbed_str)
        _audit({
            "event": "credential_scrubbed",
            "tool": tool_name,
            "note": "Credentials were detected and redacted from tool output",
        })

    _audit({
        "event": "post_tool_use",
        "tool": tool_name,
        "success": result.get("success"),
        "error": result.get("error"),
    })

    if tool_name in ("edit_remote_file", "comment_out_task", "git_commit"):
        sha = result.get("commit_sha") or result.get("sha")
        if sha:
            _commit_shas.append(sha)
            logger.info("Tracked commit %s from %s", sha[:8], tool_name)

    if tool_name == "run_dci_workflow":
        _workflow_run_count += 1
        logger.info(
            "Workflow run #%d: success=%s",
            _workflow_run_count,
            result.get("success"),
        )

    return result


def get_commit_shas() -> list[str]:
    """Return all commit SHAs tracked during this session."""
    return list(_commit_shas)


def get_workflow_run_count() -> int:
    """Return the number of workflow runs in this session."""
    return _workflow_run_count


def should_stop() -> bool:
    """Check if the agent should stop (max retries reached).
    1 initial run + MAX_FIX_ATTEMPTS retries = MAX_FIX_ATTEMPTS + 1 total.
    """
    max_total_runs = config.MAX_FIX_ATTEMPTS + 1
    return _workflow_run_count >= max_total_runs
