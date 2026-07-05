"""
Shared run_config.yml loader used by both agents/config.py and relay/config.py.

Provides the two functions that were duplicated across packages:
- find_config_file(): locate run_config.yml
- load_run_config(): parse it into a dict

Both agents and relay import from here instead of defining their own.
"""

import logging
import os

import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "run_config.yml"

REPO_ROOT_ENV_VARS = ("DCI_LOCAL_REPO_ROOT", "REPO_ROOT")


def find_config_file() -> Path:
    """Find run_config.yml by searching: env var paths, CWD, repo root, hooks dir.

    The hooks repo (private) is the primary location for run_config.yml
    since it contains environment-specific values. The main repo (public)
    has only run_config.example.yml.
    """
    candidates = [
        Path.cwd() / _CONFIG_FILENAME,
        Path(__file__).resolve().parent / _CONFIG_FILENAME,
    ]
    for env_var in REPO_ROOT_ENV_VARS:
        env_root = os.environ.get(env_var, "")
        if env_root:
            candidates.insert(0, Path(env_root) / _CONFIG_FILENAME)

    # Also search in local hooks dirs (private repo may have run_config.yml)
    repo_root = Path(__file__).resolve().parent
    for hooks_candidate in sorted(repo_root.glob("dci-hooks*")):
        if hooks_candidate.is_dir():
            candidates.append(hooks_candidate / _CONFIG_FILENAME)

    # On the jumpbox, hooks are cloned to /agentic-dci-workflow/dci-hooks/
    jumpbox_hooks = Path("/agentic-dci-workflow/dci-hooks") / _CONFIG_FILENAME
    candidates.append(jumpbox_hooks)

    for path in candidates:
        if path.exists():
            return path

    return Path.cwd() / _CONFIG_FILENAME


def load_run_config() -> dict:
    """Load run_config.yml. Returns empty dict if not found."""
    path = find_config_file()
    if not path.exists():
        logger.warning("run_config.yml not found at %s -- falling back to .env only", path)
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        logger.info("Loaded config from %s", path)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error("Failed to parse %s: %s -- falling back to .env", path, e)
        return {}


def get_value(rc: dict, yaml_key: str, env_key: str, default: str = "") -> str:
    """Get a value: run_config.yml wins, then .env, then default."""
    val = rc.get(yaml_key)
    if val is not None:
        return str(val)
    return os.environ.get(env_key, default)
