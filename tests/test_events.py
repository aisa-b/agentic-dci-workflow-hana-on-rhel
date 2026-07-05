"""Tests for agents.local.events — unified event log."""

import json

from agents.local.events import (
    emit,
    normalize_error,
    error_signature,
    search,
    get_decision_metrics,
    get_causal_chain,
    get_event_counts,
    get_events_for_run,
)


class TestNormalizeError:
    def test_strips_ansi_codes(self):
        raw = "\x1b[31mFATAL: something broke\x1b[0m"
        assert "\x1b" not in normalize_error(raw)
        assert "FATAL: something broke" in normalize_error(raw)

    def test_masks_timestamps(self):
        raw = "Error at 2026-05-29T10:30:00Z in module"
        result = normalize_error(raw)
        assert "2026-05-29" not in result
        assert "<TIMESTAMP>" in result

    def test_masks_home_paths(self):
        raw = "File /home/dci/test.conf not found"
        result = normalize_error(raw)
        assert "/home/dci/" not in result
        assert "/home/<USER>/" in result

    def test_masks_tmp_paths(self):
        raw = "Cannot read /tmp/ansible_xyz123"
        result = normalize_error(raw)
        assert "ansible_xyz123" not in result
        assert "<TMPPATH>" in result

    def test_collapses_whitespace(self):
        raw = "error   with\n\tmultiple   spaces"
        result = normalize_error(raw)
        assert "  " not in result

    def test_truncates_at_2000_chars(self):
        raw = "x" * 5000
        assert len(normalize_error(raw)) == 2000

    def test_handles_empty_string(self):
        assert normalize_error("") == ""

    def test_combined(self):
        raw = "\x1b[1m2026-01-01T00:00:00 /home/user/path error  msg\x1b[0m"
        result = normalize_error(raw)
        assert "\x1b" not in result
        assert "2026-01-01" not in result
        assert "/home/user/" not in result
        assert "  " not in result


class TestErrorSignature:
    def test_returns_16_char_hex(self):
        sig = error_signature("some error message")
        assert len(sig) == 16
        assert all(c in "0123456789abcdef" for c in sig)

    def test_same_input_same_signature(self):
        assert error_signature("error A") == error_signature("error A")

    def test_different_input_different_signature(self):
        assert error_signature("error A") != error_signature("error B")

    def test_normalization_before_hash(self):
        raw1 = "\x1b[31merror msg\x1b[0m"
        raw2 = "error msg"
        assert error_signature(raw1) == error_signature(raw2)


class TestEmit:
    def test_writes_event_to_jsonl(self, isolated_log_dir):
        event = emit("test.event", run_id="r1", data={"key": "val"})
        assert event["event_type"] == "test.event"
        assert event["run_id"] == "r1"
        assert event["event_id"]

        events_path = isolated_log_dir / "events.jsonl"
        lines = events_path.read_text().strip().splitlines()
        assert len(lines) == 1
        stored = json.loads(lines[0])
        assert stored["event_type"] == "test.event"

    def test_event_has_all_fields(self, isolated_log_dir):
        event = emit(
            "test.full",
            run_id="r1",
            target_host="target-1.example.com",
            rhel_topic="RHEL-10.2",
            attempt_number=2,
            phase=3,
            cause_event_id="prev-id",
            fix_pattern="add_missing_package",
        )
        assert event["target_host"] == "target-1.example.com"
        assert event["attempt_number"] == 2
        assert event["phase"] == 3
        assert event["cause_event_id"] == "prev-id"
        assert event["fix_pattern"] == "add_missing_package"

    def test_multiple_events_append(self, isolated_log_dir):
        emit("e1")
        emit("e2")
        emit("e3")
        events_path = isolated_log_dir / "events.jsonl"
        assert len(events_path.read_text().strip().splitlines()) == 3


class TestSearch:
    def test_substring_search(self, isolated_log_dir):
        emit("fix", data={"error": "package not found"})
        emit("fix", data={"error": "selinux denied"})
        results = search("package")
        assert len(results) == 1
        assert "package" in json.dumps(results[0]["data"])

    def test_returns_empty_for_no_match(self, isolated_log_dir):
        emit("fix", data={"error": "something"})
        results = search("nonexistent_term_xyz")
        assert results == []


class TestGetCausalChain:
    def test_traces_chain(self, isolated_log_dir):
        e1 = emit("diagnosis", data={"finding": "root cause"})
        e2 = emit("plan", cause_event_id=e1["event_id"], data={"action": "fix X"})
        e3 = emit("fix", cause_event_id=e2["event_id"], data={"sha": "abc"})

        chain = get_causal_chain(e3["event_id"])
        assert len(chain) == 3
        assert chain[0]["event_type"] == "diagnosis"
        assert chain[1]["event_type"] == "plan"
        assert chain[2]["event_type"] == "fix"

    def test_handles_missing_event(self, isolated_log_dir):
        chain = get_causal_chain("nonexistent-id")
        assert chain == []

    def test_handles_no_cause(self, isolated_log_dir):
        e = emit("standalone")
        chain = get_causal_chain(e["event_id"])
        assert len(chain) == 1

    def test_no_infinite_loop_on_cycle(self, isolated_log_dir):
        e1 = emit("a")
        e2 = emit("b", cause_event_id=e1["event_id"])
        events_path = isolated_log_dir / "events.jsonl"
        lines = events_path.read_text().strip().splitlines()
        event_a = json.loads(lines[0])
        event_a["cause_event_id"] = e2["event_id"]
        all_lines = [json.dumps(event_a)] + lines[1:]
        events_path.write_text("\n".join(all_lines) + "\n")

        chain = get_causal_chain(e2["event_id"])
        assert len(chain) <= 2


class TestGetDecisionMetrics:
    def test_empty_log(self, isolated_log_dir):
        result = get_decision_metrics()
        assert result["strategies"] == {}

    def test_groups_by_fix_pattern(self, isolated_log_dir):
        emit("fix_applied", fix_pattern="add_missing_package",
             data={"success": True}, elapsed_seconds=60)
        emit("fix_applied", fix_pattern="add_missing_package",
             data={"success": False}, elapsed_seconds=120)
        emit("fix_applied", fix_pattern="fix_storage_layout",
             data={"success": True}, elapsed_seconds=30)

        result = get_decision_metrics()
        strategies = result["strategies"]
        assert "add_missing_package" in strategies
        assert strategies["add_missing_package"]["total"] == 2
        assert strategies["add_missing_package"]["success"] == 1


class TestGetEventsForRun:
    def test_filters_by_run_id(self, isolated_log_dir):
        emit("a", run_id="run-1")
        emit("b", run_id="run-2")
        emit("c", run_id="run-1")
        result = get_events_for_run("run-1")
        assert len(result) == 2
        assert all(e["run_id"] == "run-1" for e in result)


class TestGetEventCounts:
    def test_counts_by_type(self, isolated_log_dir):
        emit("diagnosis")
        emit("diagnosis")
        emit("fix")
        result = get_event_counts(days=1)
        assert result["counts"]["diagnosis"] == 2
        assert result["counts"]["fix"] == 1
        assert result["total"] == 3
