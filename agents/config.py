"""
Central configuration for the multi-agent DCI workflow.

Configuration is split into two layers:
1. run_config.yml (in git) -- all run settings (target, model, retry, etc.)
2. .env (per-machine) -- secrets only (GCP credentials, SSH key paths)

Both the Mac agent and the relay read the same run_config.yml.
The relay picks up changes via git pull before each workflow run.
"""

import os
import logging
from pathlib import Path

from config_loader import find_config_file, load_run_config, get_value

logger = logging.getLogger(__name__)

_rc = load_run_config()


def _get(yaml_key: str, env_key: str, default: str = "") -> str:
    """Get a value: run_config.yml wins, then .env, then default."""
    return get_value(_rc, yaml_key, env_key, default)


# --- Target server ---
TARGET_HOST = _get("target", "DCI_TARGET_HOST")
TARGET_USER = _get("target_user", "DCI_TARGET_USER", "root")

# --- Jumpbox paths ---
REPO_ROOT = _get("jumpbox_repo_root", "DCI_REPO_ROOT", "/etc/dci-rhel-agent/hooks")
_target_short = TARGET_HOST.split(".")[0] if TARGET_HOST else ""
SETTINGS_FILE = os.environ.get("DCI_SETTINGS_FILE",
                               f"/etc/dci-rhel-agent/settings_current_{_target_short}.yml" if _target_short else "")
HOOKS_DIR = _get("jumpbox_hooks_dir", "DCI_HOOKS_DIR", REPO_ROOT)

# --- Local hooks repo (separate private repo) ---
LOCAL_HOOKS_DIR = _get("local_hooks_dir", "DCI_LOCAL_HOOKS_DIR", "")

# --- Local repo clone on Mac ---
LOCAL_REPO_ROOT = os.environ.get("DCI_LOCAL_REPO_ROOT", ".")

# --- Retry policy ---
MAX_FIX_ATTEMPTS = int(_get("max_fix_attempts", "DCI_MAX_FIX_ATTEMPTS", "5"))

# --- LLM (Claude via Vertex AI) ---
VERTEX_PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
VERTEX_REGION = _get("vertex_region", "CLOUD_ML_REGION", "global")
LLM_MODEL = _get("model", "DCI_LLM_MODEL")

# --- Git ---
GIT_COMMITTER_NAME = _get("git_committer_name", "DCI_GIT_COMMITTER_NAME", "DCI Agent")
GIT_COMMITTER_EMAIL = _get("git_committer_email", "DCI_GIT_COMMITTER_EMAIL", "dci-agent@localhost")
GIT_REMOTE = _get("git_remote", "DCI_GIT_REMOTE", "origin")

# --- Logging ---
LOG_DIR = Path(os.environ.get("DCI_LOG_DIR", "/tmp/dci-agent-logs"))

# --- Pub/Sub Bridge (secrets from .env, names from run_config.yml) ---
GCP_PUBSUB_PROJECT_ID = os.environ.get("GCP_PUBSUB_PROJECT_ID", "")
PUBSUB_COMMANDS_TOPIC = _get("pubsub_commands_topic", "PUBSUB_COMMANDS_TOPIC", "dci-commands")
PUBSUB_RESULTS_TOPIC = _get("pubsub_results_topic", "PUBSUB_RESULTS_TOPIC", "dci-results")
PUBSUB_RESULTS_SUB = _get("pubsub_results_sub", "PUBSUB_RESULTS_SUB", "dci-results-agent-sub")


def _validate_common() -> list[str]:
    """Checks shared by all entry points."""
    problems = []
    if not find_config_file().exists():
        problems.append(
            f"run_config.yml not found. Expected at {find_config_file()}. "
            "This file is the single source of truth for run settings."
        )
    if not GCP_PUBSUB_PROJECT_ID:
        problems.append("GCP_PUBSUB_PROJECT_ID is not set in .env (GCP project for Pub/Sub)")

    sa_key_path = os.environ.get("PUBSUB_SA_KEY_PATH", "") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if sa_key_path and not Path(sa_key_path).exists():
        problems.append(f"Pub/Sub SA key file does not exist: {sa_key_path}")
    elif not sa_key_path:
        problems.append("No Pub/Sub credentials found. Set PUBSUB_SA_KEY_PATH or GOOGLE_APPLICATION_CREDENTIALS in .env.")

    return problems


def validate() -> list[str]:
    """Return a list of configuration problems; empty means valid.

    Used by the MCP server and any direct API entry point.
    """
    problems = _validate_common()
    if not TARGET_HOST:
        problems.append(
            "Could not determine target host. Set 'target' in run_config.yml "
            "or DCI_TARGET_HOST in .env."
        )
    if not SETTINGS_FILE:
        problems.append(
            "Jumpbox settings file could not be derived. Set 'target' in run_config.yml "
            "or DCI_TARGET_HOST in .env."
        )
    if not VERTEX_PROJECT:
        problems.append("ANTHROPIC_VERTEX_PROJECT_ID is not set in .env (needed for Claude via Vertex AI)")
    if not LLM_MODEL:
        problems.append(
            "LLM model not set. Set 'model' in run_config.yml "
            "or DCI_LLM_MODEL in .env (e.g., claude-opus-4-7, claude-opus-4-6)"
        )
    return problems


def validate_mcp() -> list[str]:
    """Return a list of configuration problems for the MCP server.

    Lighter than validate() — doesn't require Vertex AI or target settings
    since those are resolved per-tool-call from run_config.yml.
    """
    return _validate_common()
