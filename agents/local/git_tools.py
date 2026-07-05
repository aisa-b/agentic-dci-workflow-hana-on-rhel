"""
Local git operations on the Mac repo clone.

All git commands run locally -- instant, no Pub/Sub round trip.
Every change is committed and pushed immediately.
"""

import datetime
import os
import subprocess

from .. import config
from ..bridge.pubsub_client import notify_relay_update

_fix_commits: list[dict] = []


def _get_repo_root() -> str:
    if hasattr(config, "LOCAL_REPO_ROOT") and config.LOCAL_REPO_ROOT:
        return config.LOCAL_REPO_ROOT
    return "."


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=_get_repo_root(),
        capture_output=True, text=True, check=check,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": config.GIT_COMMITTER_NAME,
            "GIT_AUTHOR_EMAIL": config.GIT_COMMITTER_EMAIL,
            "GIT_COMMITTER_NAME": config.GIT_COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": config.GIT_COMMITTER_EMAIL,
        },
    )


def create_fix_branch() -> dict:
    """
    Create an isolated branch for agent fixes locally.
    Called at the start of each agent run.
    """
    global _fix_commits
    _fix_commits = []
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"agent-fix/{run_id}"
    try:
        _git("checkout", "-b", branch)
        return {"success": True, "branch": branch, "run_id": run_id}
    except Exception as e:
        return {"error": str(e)}


def git_commit(message: str, files: list[str]) -> dict:
    """
    Stage specific files and commit locally. Every fix MUST be committed.

    Args:
        message: Commit message describing the fix.
        files: List of file paths to commit (relative to repo root).
    """
    try:
        for f in files:
            _git("add", f)
        attempt = len(_fix_commits) + 1
        full_msg = f"[agent-fix attempt {attempt}] {message}"
        _git("commit", "-m", full_msg)
        sha = _git("rev-parse", "HEAD").stdout.strip()
        record = {"sha": sha, "message": message, "attempt": attempt, "files": files}
        _fix_commits.append(record)
        return {"success": True, "sha": sha, "attempt": attempt, "message": full_msg}
    except Exception as e:
        return {"error": str(e)}


def git_diff() -> dict:
    """Show current uncommitted changes in the local repository."""
    result = _git("diff", check=False)
    staged = _git("diff", "--cached", check=False)
    return {
        "unstaged_diff": result.stdout[:5000],
        "staged_diff": staged.stdout[:5000],
    }


def git_push() -> dict:
    """Push the current branch to the remote, then notify relay to pull."""
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        result = _git("push", "-u", config.GIT_REMOTE, branch, check=False)
        if result.returncode != 0:
            return {"success": False, "error": f"Push failed: {result.stderr[:500]}"}
        notify_relay_update()
        return {"success": True, "branch": branch, "relay_notified": True}
    except Exception as e:
        return {"error": str(e)}


def push_and_create_pr(title: str, body: str) -> dict:
    """
    Push the current branch and create a GitHub pull request.

    Args:
        title: Pull request title.
        body: Pull request body (Markdown).
    """
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        push_result = _git("push", "-u", config.GIT_REMOTE, branch, check=False)
        if push_result.returncode != 0:
            return {"success": False, "error": f"Push failed: {push_result.stderr[:500]}"}

        notify_relay_update()

        pr_result = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body],
            cwd=_get_repo_root(),
            capture_output=True, text=True, check=False,
        )
        if pr_result.returncode == 0:
            return {"success": True, "branch": branch, "pr_url": pr_result.stdout.strip(), "relay_notified": True}
        return {
            "success": False,
            "branch": branch,
            "error": f"Branch pushed but PR creation failed: {pr_result.stderr[:500]}",
            "note": "Create the PR manually.",
        }
    except Exception as e:
        return {"error": str(e)}


def revert_all_fixes() -> dict:
    """
    Revert all agent fix commits in reverse order.
    Each revert is a NEW commit (no history rewriting).
    """
    if not _fix_commits:
        return {"message": "No fix commits to revert.", "reverted": 0}

    results = []
    for record in reversed(_fix_commits):
        r = _git("revert", "--no-edit", record["sha"], check=False)
        if r.returncode == 0:
            results.append(f"Reverted {record['sha'][:8]}: {record['message']}")
        else:
            _git("revert", "--abort", check=False)
            results.append(f"Could not auto-revert {record['sha'][:8]}: {r.stderr.strip()}")

    return {"reverted": len(results), "details": results}


def get_fix_history() -> dict:
    """Return the list of all fix commits made in this run."""
    return {"fix_count": len(_fix_commits), "commits": _fix_commits}
