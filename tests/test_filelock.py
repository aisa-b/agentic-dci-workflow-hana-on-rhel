"""Tests for agents/local/filelock.py — atomic writes and locked append."""

import json
import threading

from agents.local.filelock import atomic_write_json, locked_append


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"key": "value", "count": 42}
        atomic_write_json(path, data)

        loaded = json.loads(path.read_text())
        assert loaded == data

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "test.json"
        atomic_write_json(path, {"ok": True})
        assert path.exists()
        assert json.loads(path.read_text()) == {"ok": True}

    def test_overwrites_existing_file(self, tmp_path):
        path = tmp_path / "test.json"
        atomic_write_json(path, {"version": 1})
        atomic_write_json(path, {"version": 2})
        assert json.loads(path.read_text()) == {"version": 2}

    def test_no_partial_write_on_os_error(self, tmp_path):
        """Verify atomic rename: if rename fails, original file is preserved."""
        path = tmp_path / "test.json"
        atomic_write_json(path, {"original": True})

        # default=str handles Unserializable, so test with a write failure
        import unittest.mock
        with unittest.mock.patch("os.replace", side_effect=OSError("disk full")):
            try:
                atomic_write_json(path, {"version": 2})
            except OSError:
                pass

        assert json.loads(path.read_text()) == {"original": True}

    def test_default_str_serialization(self, tmp_path):
        """atomic_write_json uses default=str for non-JSON types."""
        from datetime import datetime
        path = tmp_path / "test.json"
        now = datetime(2026, 5, 31, 12, 0, 0)
        atomic_write_json(path, {"ts": now})
        loaded = json.loads(path.read_text())
        assert loaded["ts"] == "2026-05-31 12:00:00"


class TestLockedAppend:
    def test_appends_line(self, tmp_path):
        path = tmp_path / "test.jsonl"
        locked_append(path, '{"event": "first"}')
        locked_append(path, '{"event": "second"}')

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"event": "first"}
        assert json.loads(lines[1]) == {"event": "second"}

    def test_adds_newline_if_missing(self, tmp_path):
        path = tmp_path / "test.jsonl"
        locked_append(path, "no newline")
        assert path.read_text().endswith("\n")

    def test_preserves_existing_newline(self, tmp_path):
        path = tmp_path / "test.jsonl"
        locked_append(path, "has newline\n")
        content = path.read_text()
        assert not content.endswith("\n\n")

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "test.jsonl"
        locked_append(path, "line1")
        assert path.exists()

    def test_concurrent_appends_no_data_loss(self, tmp_path):
        path = tmp_path / "concurrent.jsonl"
        n_threads = 10
        n_lines_per_thread = 50

        def writer(thread_id):
            for i in range(n_lines_per_thread):
                locked_append(path, json.dumps({"thread": thread_id, "line": i}))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == n_threads * n_lines_per_thread

        for line in lines:
            data = json.loads(line)
            assert "thread" in data
            assert "line" in data
