"""
Atomic file operations for concurrent-safe data stores.

Provides two primitives:
- atomic_write_json: write-to-temp-then-rename for JSON files
- locked_append: fcntl-locked append for JSONL files
"""

import fcntl
import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data, indent: int = 2):
    """Write JSON atomically: write to temp file, then rename.

    Safe against concurrent readers and crash mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def locked_append(path: Path, line):
    """Append a line to a file with an exclusive fcntl lock.

    Safe for concurrent JSONL appenders. Accepts str or dict (auto-serialized).
    """
    if not isinstance(line, str):
        line = json.dumps(line, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line if line.endswith("\n") else line + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
