"""
Local file operations on the Mac repo clone.

These run directly on the local filesystem -- no Pub/Sub, no relay, instant.
The no-delete policy is enforced here.
"""

import subprocess
from pathlib import Path

from .. import config

_LOCAL_REPO: Path | None = None


def _get_repo_root() -> Path:
    """Resolve the local repo root."""
    global _LOCAL_REPO
    if _LOCAL_REPO is None:
        root = Path(config.LOCAL_REPO_ROOT) if hasattr(config, "LOCAL_REPO_ROOT") and config.LOCAL_REPO_ROOT else Path(".")
        _LOCAL_REPO = root.resolve()
    return _LOCAL_REPO


def _safe_path(relative: str) -> Path | None:
    """Resolve a relative path within the repo root. Returns None if it escapes."""
    repo = _get_repo_root()
    resolved = (repo / relative).resolve()
    if not str(resolved).startswith(str(repo)):
        return None
    return resolved


def read_file(path: str) -> dict:
    """
    Read a file from the local DCI hooks repository.

    Args:
        path: File path relative to repo root (e.g. 'user-tests.yml').
    """
    full = _safe_path(path)
    if full is None:
        return {"error": "Path traversal blocked: path must be within the repo."}
    if not full.exists():
        return {"error": f"File not found: {path}"}
    if not full.is_file():
        return {"error": f"Not a file: {path}"}
    content = full.read_text()
    lines = content.splitlines()
    numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
    return {"path": path, "content": numbered, "line_count": len(lines)}


def list_files(directory: str = ".", pattern: str = "*") -> dict:
    """
    List files and directories in the local repository.

    Args:
        directory: Directory relative to repo root. Defaults to root.
        pattern: Glob pattern to filter results (e.g. '*.yml').
    """
    base = _safe_path(directory)
    if base is None:
        return {"error": "Path traversal blocked."}
    if not base.exists():
        return {"error": f"Directory not found: {directory}"}
    entries = sorted(base.glob(pattern))
    return {
        "directory": directory,
        "entries": [
            {"name": e.name, "type": "dir" if e.is_dir() else "file"}
            for e in entries[:100]
        ],
    }


def search_files(pattern: str, file_glob: str = "*.yml") -> dict:
    """
    Search for a text pattern across files in the local repository.

    Args:
        pattern: Text or regex pattern to search for.
        file_glob: Glob to limit which files to search.
    """
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include", file_glob, pattern, "."],
            cwd=str(_get_repo_root()),
            capture_output=True, text=True, timeout=30,
        )
        matches = result.stdout.strip().splitlines()[:50]
        return {"pattern": pattern, "match_count": len(matches), "matches": matches}
    except Exception as e:
        return {"error": str(e)}


def edit_file(path: str, original: str, replacement: str) -> dict:
    """
    Edit a file using find-and-replace. Enforces the no-delete policy.

    CRITICAL RULES:
    1. NEVER delete lines -- comment them out with '#' prefix.
    2. Add '# [AGENT-DISABLED]' before commented-out blocks.
    3. Add '# [AGENT-ADDED]' before new code.

    Args:
        path: File path relative to repo root.
        original: Exact text to find.
        replacement: Replacement text. Comment out, never delete.
    """
    full = _safe_path(path)
    if full is None:
        return {"error": "Path traversal blocked."}
    if not full.exists():
        return {"error": f"File not found: {path}"}

    content = full.read_text()
    if original not in content:
        return {"error": "Original text not found in file. Use read_file first to see exact content."}
    if content.count(original) > 1:
        return {"error": f"Original text matches {content.count(original)} locations. Provide more context."}

    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped not in replacement:
            commented = f"# [AGENT-DISABLED] {stripped}"
            alt = f"#{line}"
            if commented not in replacement and alt not in replacement:
                if not any(stripped in rline for rline in replacement.splitlines()):
                    return {
                        "error": (
                            f"BLOCKED: Line would be deleted: '{stripped[:80]}'. "
                            "You MUST comment it out (prefix with '# [AGENT-DISABLED] '), not remove it."
                        ),
                    }

    new_content = content.replace(original, replacement, 1)
    full.write_text(new_content)
    return {"success": True, "path": path, "message": f"File {path} updated."}


def comment_out_task(path: str, task_name: str) -> dict:
    """
    Comment out an Ansible task block by name with '# [AGENT-DISABLED]' marker.

    Args:
        path: File path relative to repo root.
        task_name: Exact name of the Ansible task to comment out.
    """
    full = _safe_path(path)
    if full is None:
        return {"error": "Path traversal blocked."}
    if not full.exists():
        return {"error": f"File not found: {path}"}

    lines = full.read_text().splitlines(keepends=True)
    new_lines = []
    in_target = False
    found = False
    task_indent = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if stripped.startswith("- name:") and task_name in stripped:
            in_target = True
            found = True
            task_indent = len(line) - len(stripped)
            new_lines.append(" " * task_indent + "# [AGENT-DISABLED] " + stripped)
            i += 1
            continue

        if in_target:
            if stripped == "" or stripped.startswith("#"):
                new_lines.append(line)
                i += 1
                continue
            current_indent = len(line) - len(stripped)
            if current_indent > task_indent or (
                current_indent == task_indent and not stripped.startswith("- ")
            ):
                new_lines.append("#" + line)
                i += 1
                continue
            else:
                in_target = False

        new_lines.append(line)
        i += 1

    if found:
        full.write_text("".join(new_lines))
        return {"success": True, "message": f"Commented out task '{task_name}' in {path}"}
    return {"error": f"Task '{task_name}' not found in {path}"}
