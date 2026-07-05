"""Tests for agents.local.knowledge_base — persistent fix store."""

import json
import tempfile
from pathlib import Path

from agents.local.knowledge_base import (
    classify_failure,
    classify_fix_pattern,
    FIX_PATTERNS,
    capture_server_state,
    record_fix,
    search_knowledge,
    get_knowledge_summary,
    get_category_stats,
    _KB_PATH,
)


class TestClassifyFailure:
    def test_package_resolution(self):
        assert classify_failure("Package compat-openssl11 not available") == "package_resolution"

    def test_selinux(self):
        assert classify_failure("AVC denied operation") == "selinux"

    def test_storage_layout(self):
        assert classify_failure("Failed to mount /hana/data filesystem") == "storage_layout"

    def test_tuned_profile(self):
        assert classify_failure("tuned-adm profile sap-hana not found") == "tuned_profile"

    def test_service_startup(self):
        assert classify_failure("sapstartsrv failed to start, timeout waiting") == "service_startup"

    def test_network(self):
        assert classify_failure("Connection refused to target") == "network"

    def test_uncategorized(self):
        assert classify_failure("some random error with no keywords") == "uncategorized"

    def test_uses_diagnosis_too(self):
        assert classify_failure("error", "nothing provides package xyz") == "package_resolution"


class TestClassifyFixPattern:
    def test_add_missing_package(self):
        assert classify_fix_pattern("Added missing package openssl to install list") == "add_missing_package"

    def test_disable_broken_task(self):
        assert classify_fix_pattern("Comment out broken task, agent-disabled") == "disable_broken_task"

    def test_fix_variable_value(self):
        assert classify_fix_pattern("Changed variable value from X to Y") == "fix_variable_value"

    def test_fix_storage_layout(self):
        assert classify_fix_pattern("Updated disk partition layout with ondisk") == "fix_storage_layout"

    def test_fix_permissions(self):
        assert classify_fix_pattern("Set chmod executable permission on scripts") == "fix_file_permissions"

    def test_custom_fallback(self):
        assert classify_fix_pattern("did something unusual") == "custom"

    def test_fix_patterns_set_valid(self):
        assert isinstance(FIX_PATTERNS, set)
        assert len(FIX_PATTERNS) >= 10
        assert "custom" in FIX_PATTERNS


class TestCaptureServerState:
    def test_parses_rhel_version(self):
        output = "Red Hat Enterprise Linux release 9.8 (Plow)"
        state = capture_server_state(output)
        assert state["rhel_version"] == "9.8"

    def test_parses_kernel(self):
        output = "5.14.0-503.40.1.el9_5.x86_64"
        state = capture_server_state(output)
        assert "5.14.0" in state["kernel"]

    def test_parses_selinux_enforcing(self):
        state = capture_server_state("SELinux status: enforcing")
        assert state["selinux"] == "enforcing"

    def test_parses_selinux_permissive(self):
        state = capture_server_state("Current mode: permissive")
        assert state["selinux"] == "permissive"

    def test_parses_memory(self):
        state = capture_server_state("MemTotal:       131948544 kB")
        assert state["memory_gb"] == 125.8 or abs(state["memory_gb"] - 125.8) < 1

    def test_parses_tuned_profile(self):
        state = capture_server_state("Current active profile: sap-hana")
        assert state["tuned_profile"] == "sap-hana"

    def test_empty_input(self):
        state = capture_server_state("")
        assert state == {}


class TestRecordAndSearch:
    """Test recording fixes and searching the knowledge base."""

    def setup_method(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.write(b"[]")
        self._tmp.close()
        self._orig_path = _KB_PATH
        import agents.local.knowledge_base as kb
        kb._KB_PATH = Path(self._tmp.name)

    def teardown_method(self):
        import agents.local.knowledge_base as kb
        kb._KB_PATH = self._orig_path
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_record_fix_creates_entry(self):
        result = record_fix(
            error_pattern="No match for argument: compat-openssl11",
            diagnosis="Package removed in RHEL 10",
            fix_applied="Added conditional package list",
            files_changed=["config-variables.yml"],
            success=True,
            target_host="target-1.example.com",
            rhel_version="RHEL-10.2",
        )
        assert result["success"] is True
        assert result["total_entries"] == 1

    def test_record_fix_classifies_failure(self):
        record_fix(
            error_pattern="No match for argument: compat-openssl11",
            diagnosis="Package not available",
            fix_applied="Fixed package list",
            files_changed=["test.yml"],
            success=True,
        )
        import agents.local.knowledge_base as kb
        entries = json.loads(Path(kb._KB_PATH).read_text())
        assert entries[0]["failure_category"] == "package_resolution"

    def test_record_fix_deduplicates_by_sha(self):
        for _ in range(3):
            record_fix(
                error_pattern="test",
                diagnosis="test",
                fix_applied="test",
                files_changed=[],
                success=True,
                commit_sha="abc123",
            )
        import agents.local.knowledge_base as kb
        entries = json.loads(Path(kb._KB_PATH).read_text())
        assert len(entries) == 1

    def test_search_finds_match(self):
        record_fix(
            error_pattern="SELinux AVC denied write access",
            diagnosis="Missing fcontext for /hana",
            fix_applied="Added semanage fcontext",
            files_changed=["setup.yml"],
            success=True,
        )
        results = search_knowledge("SELinux AVC denied write access", threshold=0.3)
        assert results["match_count"] >= 1

    def test_search_returns_empty_for_no_match(self):
        results = search_knowledge("completely unrelated query xyz")
        assert results["match_count"] == 0

    def test_get_knowledge_summary_with_entries(self):
        record_fix(
            error_pattern="test error",
            diagnosis="test diagnosis",
            fix_applied="test fix",
            files_changed=[],
            success=True,
        )
        summary = get_knowledge_summary()
        assert "1 entries" in summary
        assert "test error" in summary

    def test_get_knowledge_summary_empty(self):
        summary = get_knowledge_summary()
        assert "No past fixes" in summary

    def test_category_stats(self):
        record_fix("SELinux denied", "selinux issue", "fixed", [], True, source="agent")
        record_fix("SELinux denied again", "selinux", "fixed", [], False, source="agent")
        record_fix("SELinux problem", "selinux", "human fix", [], True, source="human")
        stats = get_category_stats()
        assert "selinux" in stats
        assert stats["selinux"]["total"] == 3
        assert stats["selinux"]["agent_total"] == 2
        assert stats["selinux"]["human_total"] == 1
