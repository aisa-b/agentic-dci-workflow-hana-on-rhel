"""Tests for agents.local.run_journal — event-sourced run telemetry."""


from agents.local.run_journal import (
    start_run,
    end_run,
    log_workflow_dispatched,
    log_workflow_completed,
    log_triage,
    log_diagnosis,
    log_plan,
    log_fix_applied,
    log_fix_reverted,
    log_attempt_outcome,
    log_note,
    get_run_events,
    get_run_summary,
    _load_all,
)


class TestStartEndRun:
    def test_start_returns_run_id(self, isolated_log_dir):
        rid = start_run("target-1.example.com", "RHEL-10.2")
        assert rid
        assert len(rid) == 36  # UUID

    def test_end_run_records_completion(self, isolated_log_dir):
        rid = start_run("target-1.example.com", "RHEL-10.2")
        event = end_run(rid, "target-1.example.com", "RHEL-10.2",
                        success=True, total_attempts=1)
        assert event["event_type"] == "run_completed"
        assert event["data"]["success"] is True

    def test_events_written_to_journal(self, isolated_log_dir):
        rid = start_run("target-1.example.com", "RHEL-10.2")
        events = _load_all()
        assert len(events) == 1
        assert events[0]["event_type"] == "run_started"
        assert events[0]["run_id"] == rid


class TestWorkflowEvents:
    def test_log_workflow_dispatched(self, isolated_log_dir):
        event = log_workflow_dispatched("r1", "host", "topic",
                                       attempt_number=1, verbosity=2)
        assert event["data"]["verbosity"] == 2

    def test_log_workflow_completed(self, isolated_log_dir):
        event = log_workflow_completed("r1", "host", "topic",
                                      attempt_number=1, success=False,
                                      phase_reached=2, failing_task="install pkg")
        assert event["data"]["success"] is False
        assert event["data"]["phase_reached"] == 2


class TestDiagnosisEvents:
    def test_log_triage(self, isolated_log_dir):
        event = log_triage("r1", "host", "topic", attempt_number=1,
                          failing_task="yum install", error_message="not found",
                          phase=2)
        assert event["data"]["phase"] == 2

    def test_log_diagnosis_with_cause(self, isolated_log_dir):
        event = log_diagnosis("r1", "host", "topic", attempt_number=1,
                             source="dci-diagnostician",
                             findings="missing package",
                             cause_event_id="prev-event-id")
        assert event["event_type"] == "diagnosis_recorded"

    def test_log_plan_with_cause(self, isolated_log_dir):
        event = log_plan("r1", "host", "topic", attempt_number=1,
                        root_cause="missing pkg", evidence="yum output",
                        proposed_fix="add to list", confidence="high",
                        cause_event_id="diag-event-id")
        assert event["data"]["confidence"] == "high"


class TestFixEvents:
    def test_log_fix_applied(self, isolated_log_dir):
        event = log_fix_applied("r1", "host", "topic", attempt_number=1,
                               files_changed=["pre-run.yml"],
                               commit_sha="abc123")
        assert event["data"]["commit_sha"] == "abc123"

    def test_log_fix_reverted(self, isolated_log_dir):
        event = log_fix_reverted("r1", "host", "topic", attempt_number=1,
                                revert_reason="same_failure",
                                original_commit_sha="abc123")
        assert event["data"]["revert_reason"] == "same_failure"


class TestAttemptOutcome:
    def test_log_attempt_outcome(self, isolated_log_dir):
        event = log_attempt_outcome(
            "r1", "host", "topic", attempt_number=1,
            fix_sha="abc123",
            fix_description="Added missing package",
            expected_outcome="Phase 2 passes",
            actual_outcome="Same failure at phase 2",
            what_was_learned="Package name was wrong",
            keep_or_revert="reverted",
        )
        assert event["event_type"] == "attempt_outcome"
        assert event["data"]["keep_or_revert"] == "reverted"


class TestQueries:
    def test_get_run_events(self, isolated_log_dir):
        rid = start_run("host", "topic")
        log_note(rid, "host", "topic", text="test note")
        events = get_run_events(rid)
        assert len(events) == 2

    def test_get_run_summary(self, isolated_log_dir):
        rid = start_run("host", "topic")
        log_workflow_completed(rid, "host", "topic", attempt_number=1,
                              success=True, elapsed_seconds=1800)
        end_run(rid, "host", "topic", success=True, total_attempts=1)
        summary = get_run_summary(rid)
        assert summary["success"] is True
        assert len(summary["timeline"]) >= 2

    def test_summary_for_nonexistent_run(self, isolated_log_dir):
        summary = get_run_summary("nonexistent")
        assert "error" in summary
