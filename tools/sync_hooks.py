"""Sync the local hooks repo: commit pending changes and push to remote.

Called by /dci-run before dispatching a workflow (Step 0) and after
applying a fix to hooks files (Step 5d). The relay automatically pulls
the hooks repo on the jumpbox before each workflow run, so no relay
update is needed after pushing hooks.

Usage:
    python3 -m tools.sync_hooks           # sync and report
    python3 -m tools.sync_hooks --check   # dry-run: report status only
"""

import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import config

logger = logging.getLogger(__name__)


def sync_hooks(commit_message: str = "", check_only: bool = False) -> dict:
    """Push any pending hooks changes to the remote.

    Returns dict with: success, hooks_dir, status, pushed, message.
    """
    hooks_dir = config.LOCAL_HOOKS_DIR
    if not hooks_dir:
        return {
            "success": True,
            "hooks_dir": "",
            "status": "not_configured",
            "pushed": False,
            "message": "No local_hooks_dir configured in run_config.yml — skipping hooks sync.",
        }

    repo_root = Path(config.LOCAL_REPO_ROOT).resolve()
    hooks_path = (repo_root / hooks_dir).resolve()

    if not hooks_path.exists():
        return {
            "success": False,
            "hooks_dir": str(hooks_path),
            "status": "missing",
            "pushed": False,
            "message": f"Hooks directory not found: {hooks_path}",
        }

    git_dir = hooks_path / ".git"
    if not git_dir.exists():
        return {
            "success": False,
            "hooks_dir": str(hooks_path),
            "status": "not_a_repo",
            "pushed": False,
            "message": f"Hooks directory is not a git repo: {hooks_path}",
        }

    def _run(cmd):
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=str(hooks_path),
        )

    status = _run(["git", "status", "--porcelain"])
    has_changes = bool(status.stdout.strip())

    ahead = _run(["git", "rev-list", "--count", "@{upstream}..HEAD"])
    unpushed = int(ahead.stdout.strip()) if ahead.returncode == 0 else 0

    if check_only:
        return {
            "success": True,
            "hooks_dir": str(hooks_path),
            "status": "dirty" if has_changes else ("ahead" if unpushed > 0 else "clean"),
            "uncommitted_files": status.stdout.strip().splitlines() if has_changes else [],
            "unpushed_commits": unpushed,
            "pushed": False,
            "message": f"{'Uncommitted changes' if has_changes else ''}"
                       f"{' + ' if has_changes and unpushed else ''}"
                       f"{f'{unpushed} unpushed commits' if unpushed else ''}"
                       if has_changes or unpushed else "Hooks repo is clean and up to date.",
        }

    if has_changes:
        msg = commit_message or "Sync hooks before workflow run"
        add = _run(["git", "add", "-A"])
        if add.returncode != 0:
            return {
                "success": False, "hooks_dir": str(hooks_path),
                "status": "error", "pushed": False,
                "message": f"git add failed: {add.stderr[:300]}",
            }
        commit = _run(["git", "commit", "-m", msg])
        if commit.returncode != 0:
            return {
                "success": False, "hooks_dir": str(hooks_path),
                "status": "error", "pushed": False,
                "message": f"git commit failed: {commit.stderr[:300]}",
            }
        logger.info("Committed hooks changes: %s", msg)

    ahead2 = _run(["git", "rev-list", "--count", "@{upstream}..HEAD"])
    unpushed2 = int(ahead2.stdout.strip()) if ahead2.returncode == 0 else 0

    if unpushed2 == 0:
        return {
            "success": True, "hooks_dir": str(hooks_path),
            "status": "clean", "pushed": False,
            "message": "Hooks repo already up to date — nothing to push.",
        }

    push = _run(["git", "push", "origin", "HEAD"])
    if push.returncode != 0:
        return {
            "success": False, "hooks_dir": str(hooks_path),
            "status": "push_failed", "pushed": False,
            "message": f"git push failed: {push.stderr[:300]}",
        }

    logger.info("Pushed %d commit(s) to hooks repo", unpushed2)
    return {
        "success": True,
        "hooks_dir": str(hooks_path),
        "status": "pushed",
        "pushed": True,
        "commits_pushed": unpushed2,
        "message": f"Pushed {unpushed2} commit(s) to hooks repo.",
    }


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Sync hooks repo")
    parser.add_argument("--check", action="store_true", help="Dry-run: report status only")
    parser.add_argument("--message", "-m", default="", help="Commit message")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = sync_hooks(commit_message=args.message, check_only=args.check)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
