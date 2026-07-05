"""Shared fixtures for DCI agent tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_log_dir(tmp_path):
    """Redirect all file-based stores to a temp directory per test."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    patches = [
        patch("agents.local.events._LOG_DIR", log_dir),
        patch("agents.local.events._EVENTS_PATH", log_dir / "events.jsonl"),
        patch("agents.local.run_journal._LOG_DIR", log_dir),
        patch("agents.local.run_journal._JOURNAL_PATH", log_dir / "run_journal.jsonl"),
    ]

    for p in patches:
        p.start()

    yield log_dir

    for p in patches:
        p.stop()


@pytest.fixture
def sample_kb_entries():
    return [
        {
            "timestamp": "2026-05-01T10:00:00",
            "error_pattern": "Package compat-openssl11 not available",
            "diagnosis": "Missing package in RHEL 10",
            "fix_applied": "Added compat-openssl11 to yum install list",
            "files_changed": ["dci-hooks/pre-run.yml"],
            "success": True,
            "failure_category": "package_resolution",
            "fix_pattern": "add_missing_package",
            "source": "agent",
            "server_state": {},
            "outcome": {"phase_reached": 4, "tasks_passed": 499, "attempt_number": 1},
            "commit_sha": "abc123",
            "run_id": "run-001",
            "_embedding": [],
        },
        {
            "timestamp": "2026-05-02T10:00:00",
            "error_pattern": "tuned profile sap-hana not found",
            "diagnosis": "tuned-profiles-sap-hana package missing",
            "fix_applied": "Added tuned-profiles-sap-hana to package list",
            "files_changed": ["dci-hooks/config-variables.yml"],
            "success": False,
            "failure_category": "tuned_profile",
            "fix_pattern": "add_missing_package",
            "source": "agent",
            "server_state": {},
            "outcome": {"phase_reached": 2, "tasks_passed": 100, "attempt_number": 2},
            "commit_sha": "def456",
            "run_id": "run-002",
            "_embedding": [],
        },
    ]
