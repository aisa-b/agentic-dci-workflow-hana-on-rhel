"""Tests for the fix loop handoff validation gates."""

import json
from unittest.mock import patch

import pytest

from agents.local import fix_loop


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    state_file = tmp_path / "fix_loop_state.json"
    monkeypatch.setattr(fix_loop, "_STATE_FILE", state_file)
    yield state_file


def _parse(result_json):
    return json.loads(result_json)


class TestStartFixLoop:
    def test_creates_active_state(self):
        r = _parse(fix_loop.start_fix_loop("host.example.com", "RHEL-9.8", "error output"))
        assert r["accepted"] is True
        assert r["state"] == "TRIAGE"
        state = fix_loop._load()
        assert state["active"] is True
        assert state["attempt"] == 1

    def test_truncates_error_output(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "x" * 5000)
        state = fix_loop._load()
        assert len(state["error_output"]) == 2000


class TestSubmitTriage:
    def _start(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "disk not found")

    def test_rejects_without_active_loop(self):
        r = _parse(fix_loop.submit_triage(action_type="file_fix"))
        assert r["accepted"] is False
        assert "No active fix loop" in r["error"]

    def test_rejects_empty_action_type(self):
        self._start()
        r = _parse(fix_loop.submit_triage())
        assert r["accepted"] is False
        assert "Missing action_type" in r["error"]

    def test_rejects_unknown_action_type(self):
        self._start()
        r = _parse(fix_loop.submit_triage(action_type="magic"))
        assert r["accepted"] is False
        assert "Unknown action_type" in r["error"]

    def test_rejects_missing_common_fields(self):
        self._start()
        r = _parse(fix_loop.submit_triage(
            action_type="file_fix",
            file_path="setup.yml", line=260, correct_value="scsi-358",
        ))
        assert r["accepted"] is False
        assert "failing_task" in r["missing"]
        assert "phase" in r["missing"]
        assert "evidence" in r["missing"]

    def test_rejects_missing_file_fix_fields(self):
        self._start()
        r = _parse(fix_loop.submit_triage(
            action_type="file_fix",
            failing_task="disk-init", phase=3, evidence="grep output",
        ))
        assert r["accepted"] is False
        assert "file_path" in r["missing"]
        assert "correct_value" in r["missing"]
        assert len(r["hints"]) > 0

    def test_accepts_complete_file_fix(self):
        self._start()
        r = _parse(fix_loop.submit_triage(
            action_type="file_fix",
            file_path="setup.yml", line=260,
            wrong_value="scsi-360", correct_value="scsi-358",
            evidence="line 264 has correct ID", source="local_analysis",
            failing_task="disk-init", phase=3,
        ))
        assert r["accepted"] is True
        assert r["state"] == "PLAN"
        state = fix_loop._load()
        assert state["triage_accepted"] is True

    def test_accepts_config_change(self):
        self._start()
        r = _parse(fix_loop.submit_triage(
            action_type="config_change",
            parameter="tuned_profile", target_value="sap-hana",
            evidence="tuned-adm active shows wrong profile",
            failing_task="tuned-check", phase=2,
        ))
        assert r["accepted"] is True

    def test_accepts_infrastructure(self):
        self._start()
        r = _parse(fix_loop.submit_triage(
            action_type="infrastructure",
            component="memory", remediation_steps=["increase VM RAM to 256GB"],
            evidence="dmesg shows OOM killer", source="dci-diagnostician",
            failing_task="hana-install", phase=3,
        ))
        assert r["accepted"] is True

    def test_rejects_escalation_without_subagent(self):
        self._start()
        r = _parse(fix_loop.submit_triage(
            action_type="escalate_to_human",
            description="unknown regression", why_agent_cannot_fix="no fix found",
            evidence="all subagents failed", source="combined",
            failing_task="unknown", phase=2,
        ))
        assert r["accepted"] is False
        assert "subagent" in r["error"].lower()

    def test_accepts_escalation_after_subagent(self):
        self._start()
        fix_loop.mark_subagent_used()
        r = _parse(fix_loop.submit_triage(
            action_type="escalate_to_human",
            description="unknown regression", why_agent_cannot_fix="no fix found",
            evidence="diagnostician investigated, no root cause",
            source="dci-diagnostician",
            failing_task="unknown", phase=2,
        ))
        assert r["accepted"] is True

    def test_rejects_duplicate_triage(self):
        self._start()
        fix_loop.submit_triage(
            action_type="file_fix", file_path="f", line=1,
            correct_value="v", evidence="e",
            failing_task="t", phase=1,
        )
        r = _parse(fix_loop.submit_triage(action_type="file_fix"))
        assert r["accepted"] is False
        assert "already accepted" in r["error"]

    def test_operator_hint_recorded(self):
        self._start()
        fix_loop.submit_triage(
            action_type="file_fix", file_path="f", line=1,
            correct_value="v", evidence="e",
            failing_task="t", phase=1,
            operator_hint="check the comments in setup.yml",
        )
        state = fix_loop._load()
        assert "check the comments" in state["operator_hints"][0]


class TestSubmitPlan:
    def _triage(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        fix_loop.submit_triage(
            action_type="file_fix", file_path="f", line=1,
            correct_value="v", evidence="e",
            failing_task="t", phase=1,
        )

    def test_rejects_without_triage(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        r = _parse(fix_loop.submit_plan(root_cause="x", proposed_fix="y", confidence="high"))
        assert r["accepted"] is False
        assert "Triage not accepted" in r["error"]

    def test_rejects_missing_fields(self):
        self._triage()
        r = _parse(fix_loop.submit_plan(root_cause="x"))
        assert r["accepted"] is False
        assert "proposed_fix" in r["missing"]

    def test_accepts_complete_plan(self):
        self._triage()
        r = _parse(fix_loop.submit_plan(
            root_cause="wrong disk IDs",
            proposed_fix="swap IDs in setup.yml",
            confidence="high",
            fallback="SSH to check actual disks",
            risk="low",
        ))
        assert r["accepted"] is True
        assert r["state"] == "FIX"


class TestSubmitFix:
    def _plan(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        fix_loop.submit_triage(
            action_type="file_fix", file_path="f", line=1,
            correct_value="v", evidence="e",
            failing_task="t", phase=1,
        )
        fix_loop.submit_plan(
            root_cause="x", proposed_fix="y",
            confidence="high", fallback="z", risk="low",
        )

    def test_rejects_without_plan(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        r = _parse(fix_loop.submit_fix(commit_sha="abc123"))
        assert r["accepted"] is False
        assert "Plan not accepted" in r["error"]

    def test_rejects_no_sha(self):
        self._plan()
        r = _parse(fix_loop.submit_fix())
        assert r["accepted"] is False
        assert "No commit_sha" in r["error"]

    def test_rejects_nonexistent_sha(self):
        self._plan()
        with patch.object(fix_loop, "_git_commit_exists", return_value=False):
            r = _parse(fix_loop.submit_fix(commit_sha="nonexistent"))
        assert r["accepted"] is False
        assert "not found in git" in r["error"]

    def test_rejects_no_review(self):
        self._plan()
        with patch.object(fix_loop, "_git_commit_exists", return_value=True):
            r = _parse(fix_loop.submit_fix(commit_sha="abc123"))
        assert r["accepted"] is False
        assert "review_verdict" in r["error"]

    def test_rejects_reject_verdict(self):
        self._plan()
        with patch.object(fix_loop, "_git_commit_exists", return_value=True):
            r = _parse(fix_loop.submit_fix(commit_sha="abc123", review_verdict="REJECT"))
        assert r["accepted"] is False
        assert "REJECT" in r["error"]

    def test_accepts_with_approve(self):
        self._plan()
        with patch.object(fix_loop, "_git_commit_exists", return_value=True):
            r = _parse(fix_loop.submit_fix(
                commit_sha="abc123", files_changed=["setup.yml"],
                description="swapped disk IDs", review_verdict="APPROVE",
            ))
        assert r["accepted"] is True
        assert r["state"] == "VERIFY"
        state = fix_loop._load()
        assert state["fix_committed"] is True
        assert state["review_approved"] is True


class TestSubmitResult:
    def _fix(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        fix_loop.submit_triage(
            action_type="file_fix", file_path="f", line=1,
            correct_value="v", evidence="e",
            failing_task="t", phase=1,
        )
        fix_loop.submit_plan(
            root_cause="x", proposed_fix="y",
            confidence="high", fallback="z", risk="low",
        )
        with patch.object(fix_loop, "_git_commit_exists", return_value=True):
            fix_loop.submit_fix(
                commit_sha="abc123", files_changed=["f"],
                description="d", review_verdict="APPROVE",
            )

    def test_rejects_without_fix(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        r = _parse(fix_loop.submit_result(success=True))
        assert r["accepted"] is False

    def test_success_ends_loop(self):
        self._fix()
        r = _parse(fix_loop.submit_result(success=True, phase_reached=5))
        assert r["accepted"] is True
        assert r["done"] is True
        assert r["success"] is True
        state = fix_loop._load()
        assert state["active"] is False

    def test_rejects_failure_without_assessment(self):
        self._fix()
        r = _parse(fix_loop.submit_result(success=False, phase_reached=3))
        assert r["accepted"] is False
        assert "progress_assessment" in r["error"]

    def test_rejects_assessment_without_evidence(self):
        self._fix()
        r = _parse(fix_loop.submit_result(
            success=False, phase_reached=3,
            progress_assessment="same",
        ))
        assert r["accepted"] is False
        assert "assessment_evidence" in r["error"]

    def test_failure_resets_for_next_attempt(self):
        self._fix()
        r = _parse(fix_loop.submit_result(
            success=False, phase_reached=3,
            progress_assessment="same", assessment_evidence="same error",
            error_summary="new error output",
        ))
        assert r["accepted"] is True
        assert r["done"] is False
        assert r["attempt"] == 2
        state = fix_loop._load()
        assert state["triage_accepted"] is False
        assert state["plan_accepted"] is False
        assert state["fix_committed"] is False
        assert len(state["attempt_summaries"]) == 1

    def test_progress_keeps_fix(self):
        self._fix()
        r = _parse(fix_loop.submit_result(
            success=False, phase_reached=4,
            progress_assessment="progress",
            assessment_evidence="moved from phase 3 to 4",
        ))
        assert r["kept"] is True

    def test_regression_reverts_fix(self):
        self._fix()
        r = _parse(fix_loop.submit_result(
            success=False, phase_reached=2,
            progress_assessment="regression",
            assessment_evidence="moved from phase 3 to 2",
        ))
        assert r["kept"] is False
        assert "revert" in r["revert_instruction"].lower()

    def test_max_attempts_ends_loop(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        state = fix_loop._load()
        state["attempt"] = 5
        state["fix_committed"] = True
        state["fix_data"] = {"commit_sha": "x", "files_changed": [], "description": "d"}
        fix_loop._save(state)

        r = _parse(fix_loop.submit_result(
            success=False, phase_reached=3,
            progress_assessment="same", assessment_evidence="still failing",
        ))
        assert r["done"] is True
        assert r["success"] is False

    def test_attempt_summary_created(self):
        self._fix()
        fix_loop.submit_result(
            success=False, phase_reached=3,
            progress_assessment="same", assessment_evidence="same error",
        )
        state = fix_loop._load()
        summary = state["attempt_summaries"][0]
        assert summary["attempt"] == 1
        assert summary["fix_outcome"] == "same"
        assert summary["fix_kept"] is False


class TestCheckStuck:
    def test_not_stuck_with_few_attempts(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        r = json.loads(fix_loop.check_stuck())
        assert r["stuck"] is False

    def test_stuck_same_root_cause_3_times(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        state = fix_loop._load()
        state["attempt_summaries"] = [
            {"root_cause": "same issue", "fix_outcome": "same"},
            {"root_cause": "same issue", "fix_outcome": "same"},
            {"root_cause": "same issue", "fix_outcome": "same"},
        ]
        fix_loop._save(state)
        r = json.loads(fix_loop.check_stuck())
        assert r["stuck"] is True
        assert "repeated" in r["reason"]

    def test_stuck_same_outcome_twice(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        state = fix_loop._load()
        state["attempt_summaries"] = [
            {"root_cause": "a", "fix_outcome": "same"},
            {"root_cause": "b", "fix_outcome": "same"},
        ]
        fix_loop._save(state)
        r = json.loads(fix_loop.check_stuck())
        assert r["stuck"] is True


class TestEndFixLoop:
    def test_deactivates(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        fix_loop.end_fix_loop()
        state = fix_loop._load()
        assert state["active"] is False


class TestGetFixLoop:
    def test_returns_none_when_inactive(self):
        assert fix_loop.get_fix_loop() is None

    def test_returns_state_when_active(self):
        fix_loop.start_fix_loop("host", "RHEL-9.8", "error")
        state = fix_loop.get_fix_loop()
        assert state is not None
        assert state["active"] is True
