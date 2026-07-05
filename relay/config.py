"""
Relay daemon configuration.

Configuration is split into two layers:
1. run_config.yml (in git) -- all run settings (target, model, retry, etc.)
2. .env (per-machine) -- secrets only (GCP credentials, SSH key paths)

The relay reads run_config.yml from the local repo clone. Since the relay
does git pull before each workflow run, config changes propagate automatically
when you push from your Mac.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from config_loader import load_run_config, get_value

logger = logging.getLogger(__name__)

_rc = load_run_config()


def _get(yaml_key: str, env_key: str, default: str = "") -> str:
    """Get a value: run_config.yml wins, then .env, then default."""
    return get_value(_rc, yaml_key, env_key, default)


# --- Google Cloud Pub/Sub (project ID is a secret, names are in run_config) ---
GCP_PUBSUB_PROJECT_ID = os.environ.get("GCP_PUBSUB_PROJECT_ID", "")
PUBSUB_COMMANDS_TOPIC = _get("pubsub_commands_topic", "PUBSUB_COMMANDS_TOPIC", "dci-commands")
PUBSUB_RESULTS_TOPIC = _get("pubsub_results_topic", "PUBSUB_RESULTS_TOPIC", "dci-results")
PUBSUB_COMMANDS_SUB = _get("pubsub_commands_sub", "PUBSUB_COMMANDS_SUB", "dci-commands-relay-sub")

# --- Jumpbox SSH (host from run_config, key path from .env) ---
JUMPBOX_HOST = _get("jumpbox_host", "JUMPBOX_HOST")
JUMPBOX_USER = _get("jumpbox_user", "JUMPBOX_USER", "")
JUMPBOX_SSH_KEY = os.environ.get("JUMPBOX_SSH_KEY", "")

# --- Target server ---
TARGET_HOST = _get("target", "DCI_TARGET_HOST")
TARGET_USER = _get("target_user", "DCI_TARGET_USER", "root")
TARGET_PASSWORD = _get("target_password", "DCI_TARGET_PASSWORD", "fallback_password")

# --- Paths on the jumpbox ---
REPO_ROOT = _get("jumpbox_repo_root", "DCI_REPO_ROOT", "/etc/dci-rhel-agent/hooks")
_target_short = TARGET_HOST.split(".")[0] if TARGET_HOST else ""
SETTINGS_FILE = os.environ.get("DCI_SETTINGS_FILE",
                               f"/etc/dci-rhel-agent/settings_current_{_target_short}.yml" if _target_short else "")
HOOKS_DIR = _get("jumpbox_hooks_dir", "DCI_HOOKS_DIR", REPO_ROOT)

# --- Git identity ---
GIT_COMMITTER_NAME = _get("git_committer_name", "DCI_GIT_COMMITTER_NAME", "DCI Agent")
GIT_COMMITTER_EMAIL = _get("git_committer_email", "DCI_GIT_COMMITTER_EMAIL", "dci-agent@localhost")
GIT_REMOTE = _get("git_remote", "DCI_GIT_REMOTE", "origin")
GITHUB_REMOTE_URL = _get("github_remote_url", "GITHUB_REMOTE_URL", "")

# --- Safety limits ---
MAX_FIX_ATTEMPTS = int(_get("max_fix_attempts", "DCI_MAX_FIX_ATTEMPTS", "5"))
SESSION_TIMEOUT_HOURS = int(_get("relay_session_timeout_hours", "RELAY_SESSION_TIMEOUT_HOURS", "12"))
AUDIT_LOG = os.environ.get(
    "RELAY_AUDIT_LOG",
    str(Path.cwd() / "audit.jsonl"),
)
WORKFLOW_TIMEOUT = int(_get("workflow_timeout_seconds", "DCI_WORKFLOW_TIMEOUT", "7200"))


def get_target_password(target_host: str = "") -> str:
    """Get the password for a specific target server.

    Checks servers.<hostname>.target_password first, then falls back
    to the global target_password. This allows per-server overrides
    (e.g. servers that haven't been redeployed yet use a different password).
    """
    if target_host:
        servers = _rc.get("servers", {})
        for name, srv in servers.items():
            fqdn = srv.get("fqdn", "")
            if fqdn == target_host or name == target_host:
                per_server_pw = srv.get("target_password", "")
                if per_server_pw:
                    return str(per_server_pw)
    return TARGET_PASSWORD


def reload_run_config() -> dict:
    """Re-read run_config.yml and update module globals.

    Called by the workflow handler after git pull so changes
    take effect without a daemon restart.
    """
    global _rc, TARGET_HOST, TARGET_USER, TARGET_PASSWORD
    global SETTINGS_FILE, HOOKS_DIR, REPO_ROOT
    global WORKFLOW_TIMEOUT, MAX_FIX_ATTEMPTS

    _rc = load_run_config()

    TARGET_HOST = _get("target", "DCI_TARGET_HOST")
    TARGET_USER = _get("target_user", "DCI_TARGET_USER", "root")
    TARGET_PASSWORD = _get("target_password", "DCI_TARGET_PASSWORD", "fallback_password")
    REPO_ROOT = _get("jumpbox_repo_root", "DCI_REPO_ROOT", "/etc/dci-rhel-agent/hooks")
    _short = TARGET_HOST.split(".")[0] if TARGET_HOST else ""
    SETTINGS_FILE = os.environ.get("DCI_SETTINGS_FILE",
                                   f"/etc/dci-rhel-agent/settings_current_{_short}.yml" if _short else "")
    HOOKS_DIR = _get("jumpbox_hooks_dir", "DCI_HOOKS_DIR", REPO_ROOT)
    WORKFLOW_TIMEOUT = int(_get("workflow_timeout_seconds", "DCI_WORKFLOW_TIMEOUT", "7200"))
    MAX_FIX_ATTEMPTS = int(_get("max_fix_attempts", "DCI_MAX_FIX_ATTEMPTS", "5"))

    logger.info("Reloaded run_config.yml: target=%s, settings=%s", TARGET_HOST, SETTINGS_FILE)
    return _rc


def validate() -> list[str]:
    """Return a list of configuration problems; empty means valid."""
    problems = []
    if not GCP_PUBSUB_PROJECT_ID:
        problems.append("GCP_PUBSUB_PROJECT_ID is not set in .env")
    if not JUMPBOX_HOST:
        problems.append("Jumpbox host not set. Set 'jumpbox_host' in run_config.yml or JUMPBOX_HOST in .env")
    if not JUMPBOX_SSH_KEY:
        problems.append("JUMPBOX_SSH_KEY is not set in .env (path to SSH key for jumpbox)")
    # TARGET_HOST and SETTINGS_FILE are not validated here — they are
    # passed per-command and not required at relay startup.
    return problems
