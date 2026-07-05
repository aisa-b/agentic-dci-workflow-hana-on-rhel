"""
Synchronize DCI settings: generate from run_config.yml.

The generated file is read by the MCP server and embedded in the Pub/Sub
workflow.run payload. The relay extracts it and deploys via SFTP.
No git push or cloud storage needed — settings ride in the message.
"""

import logging
import subprocess
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _short_hostname(hostname: str) -> str:
    return hostname.split(".")[0]


def _load_run_config(repo_root: Path) -> dict:
    path = repo_root / "run_config.yml"
    with open(path) as f:
        return yaml.safe_load(f) or {}


def sync_settings_for_target(
    target_host: str,
    topic: str = "",
    repo_root: str = "",
) -> dict:
    """Generate settings for target_host.

    The MCP server reads the generated file and includes it in the
    Pub/Sub payload. No external distribution needed.

    Returns dict with: success, settings_file, target_host, topic,
    generated_path, error.
    """
    root = Path(repo_root) if repo_root else _REPO_ROOT

    try:
        config = _load_run_config(root)
    except Exception as e:
        return {"success": False, "error": f"Failed to load run_config.yml: {e}"}

    servers = config.get("servers", {})
    disk_map = config.get("disk_map", {})
    short = _short_hostname(target_host)

    if short not in servers:
        return {
            "success": False,
            "error": f"Server '{short}' not found in run_config.yml servers section.",
        }

    server = servers[short]
    domain = config.get("domain", "example.corp")
    fqdn = server.get("fqdn", f"{short}.{domain}")

    if not topic:
        return {
            "success": False,
            "error": f"No topic specified for '{short}'. Topic is required.",
        }

    disk = disk_map.get(short)
    if not disk:
        return {
            "success": False,
            "error": (
                f"Server '{short}' has no disk mapping in disk_map. "
                f"Run: /dci-configure --discover {short}"
            ),
        }

    gen_result = subprocess.run(
        [sys.executable, "-m", "tools.configure_target", "generate", short, topic],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if gen_result.returncode != 0:
        return {
            "success": False,
            "error": f"Settings generation failed for {short} with topic {topic}: {gen_result.stderr[:500]}",
        }
    generated_path = root / "settings" / f"settings_current_{short}.yml"

    settings_file = f"/etc/dci-rhel-agent/settings_current_{short}.yml"

    logger.info("Settings generated for %s: %s", short, generated_path)

    return {
        "success": True,
        "settings_file": settings_file,
        "target_host": fqdn,
        "topic": topic,
        "generated_path": str(generated_path),
    }
