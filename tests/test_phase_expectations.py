"""Tests for agents.local.phase_expectations — phase world model."""

import json
from unittest.mock import patch

from agents.local.phase_expectations import (
    check_phase_expectations,
    detect_phase_number,
    get_phase_timing,
    get_server_phase_timing,
    is_phase_overdue,
    format_phase_report,
    PHASE_EXPECTATIONS,
    PHASE_PATTERNS,
)


class TestPhaseExpectations:
    def test_all_five_phases_defined(self):
        for phase in [1, 2, 3, 4, 5]:
            assert phase in PHASE_EXPECTATIONS
            assert "name" in PHASE_EXPECTATIONS[phase]
            assert "expected_state" in PHASE_EXPECTATIONS[phase]
            assert "typical_duration_minutes" in PHASE_EXPECTATIONS[phase]

    def test_unknown_phase(self):
        result = check_phase_expectations(99, {})
        assert "error" in result


class TestCheckPhaseExpectations:
    def test_all_met(self):
        actual = {
            "tuned_profile": "sap-hana",
            "kernel_params_set": True,
            "selinux_mode": "permissive",
            "sap_packages_installed": True,
            "hana_filesystems_mounted": True,
        }
        result = check_phase_expectations(2, actual)
        assert result["all_met"] is True
        assert len(result["deviations"]) == 0
        assert len(result["missing"]) == 0

    def test_deviation_detected(self):
        actual = {"tuned_profile": "sap-hana", "selinux_mode": "enforcing"}
        result = check_phase_expectations(2, actual)
        assert result["all_met"] is False
        deviations = result["deviations"]
        selinux_dev = [d for d in deviations if d["check"] == "selinux_mode"]
        assert len(selinux_dev) == 1
        assert selinux_dev[0]["expected"] == "permissive"
        assert selinux_dev[0]["actual"] == "enforcing"

    def test_missing_checks(self):
        result = check_phase_expectations(2, {})
        assert len(result["missing"]) == 5

    def test_phase_1_ssh(self):
        result = check_phase_expectations(1, {"ssh_accessible": True, "os_installed": False})
        assert "ssh_accessible" in result["met"]
        devs = [d for d in result["deviations"] if d["check"] == "os_installed"]
        assert len(devs) == 1


class TestGetPhaseTiming:
    def test_returns_timing(self):
        timing = get_phase_timing(1)
        assert timing["typical_minutes"] == 30
        assert timing["max_minutes"] == 50

    def test_unknown_phase(self):
        assert "error" in get_phase_timing(99)


class TestIsPhaseOverdue:
    def test_not_overdue(self):
        assert is_phase_overdue(1, 20) is False

    def test_overdue(self):
        assert is_phase_overdue(1, 65) is True

    def test_unknown_phase_not_overdue(self):
        assert is_phase_overdue(99, 9999) is False


class TestFormatPhaseReport:
    def test_produces_readable_output(self):
        report = format_phase_report(2, {"tuned_profile": "sap-hana"}, elapsed_minutes=15)
        assert "Phase 2" in report
        assert "OS Prep for HANA" in report
        assert "on track" in report

    def test_overdue_report(self):
        report = format_phase_report(2, {}, elapsed_minutes=50)
        assert "OVERDUE" in report

    def test_report_with_target_host(self):
        report = format_phase_report(2, {"tuned_profile": "sap-hana"}, elapsed_minutes=15, target_host="unknown-server")
        assert "Phase 2" in report
        assert "on track" in report


class TestDetectPhaseNumber:
    def test_os_deployment(self):
        assert detect_phase_number("play:OS Deployment") == 1
        assert detect_phase_number("play:Deploy RHEL on bare metal") == 1

    def test_sap_prep(self):
        assert detect_phase_number("task:sap-preconfigure : Ensure packages") == 2
        assert detect_phase_number("play:SAP HANA Preconfigure") == 2
        assert detect_phase_number("task:Set tuned profile") == 2

    def test_hana_install(self):
        assert detect_phase_number("play:Install SAP HANA") == 3
        assert detect_phase_number("task:Run hdblcm installer") == 3

    def test_benchmark(self):
        assert detect_phase_number("play:PBOffline benchmark") == 4
        assert detect_phase_number("task:Run PBO test") == 4

    def test_results(self):
        assert detect_phase_number("play:Collect results") == 5
        assert detect_phase_number("task:Upload JUnit report") == 5

    def test_unknown(self):
        assert detect_phase_number("something unknown") is None

    def test_empty(self):
        assert detect_phase_number("") is None

    def test_case_insensitive(self):
        assert detect_phase_number("PLAY:OS DEPLOYMENT") == 1
        assert detect_phase_number("task:SAP-PRECONFIGURE") == 2

    def test_all_phases_have_patterns(self):
        for phase in [1, 2, 3, 4, 5]:
            assert phase in PHASE_PATTERNS
            assert len(PHASE_PATTERNS[phase]) > 0


class TestGetServerPhaseTiming:
    def test_falls_back_to_static_when_no_timings_file(self, tmp_path):
        with patch("agents.local.phase_expectations._get_phase_timings_path", return_value=tmp_path / "nonexistent.jsonl"):
            timing = get_server_phase_timing("target-1.example.com", 2)
            assert timing["source"] == "static_default"
            assert timing["typical_minutes"] == 15
            assert timing["data_points"] == 0

    def test_falls_back_when_insufficient_data(self, tmp_path):
        timings_file = tmp_path / "phase_timings.json"
        db = {
            "target-1:RHEL-10.2": {
                "run_count": 1,
                "phase_averages": {"1": 1800, "2": 900, "3": 1200},
            }
        }
        timings_file.write_text(json.dumps(db))

        with patch("agents.local.phase_expectations._get_phase_timings_path", return_value=timings_file):
            timing = get_server_phase_timing("target-1.example.com", 2, rhel_topic="RHEL-10.2")
            assert timing["source"] == "static_default"
            assert timing["data_points"] == 1

    def test_uses_learned_timing_with_enough_data(self, tmp_path):
        timings_file = tmp_path / "phase_timings.json"
        db = {
            "target-1:RHEL-10.2": {
                "run_count": 4,
                "phase_averages": {"1": 1800, "2": 960, "3": 1350},
            }
        }
        timings_file.write_text(json.dumps(db))

        with patch("agents.local.phase_expectations._get_phase_timings_path", return_value=timings_file):
            timing = get_server_phase_timing("target-1.example.com", 2, rhel_topic="RHEL-10.2")
            assert timing["source"] == "learned"
            assert timing["data_points"] == 4
            assert timing["typical_minutes"] > 0
            assert timing["max_minutes"] >= timing["typical_minutes"]

    def test_filters_by_host(self, tmp_path):
        timings_file = tmp_path / "phase_timings.json"
        db = {
            "target-1:": {
                "run_count": 3,
                "phase_averages": {"1": 660, "2": 900},
            },
            "target-2:": {
                "run_count": 3,
                "phase_averages": {"1": 660, "2": 9000},
            },
        }
        timings_file.write_text(json.dumps(db))

        with patch("agents.local.phase_expectations._get_phase_timings_path", return_value=timings_file):
            timing = get_server_phase_timing("target-1.example.com", 2)
            assert timing["source"] == "learned"
            assert timing["data_points"] == 3
            assert timing["typical_minutes"] < 25


class TestIsPhaseOverdueWithHost:
    def test_uses_static_for_unknown_host(self):
        assert is_phase_overdue(1, 60, target_host="unknown-host") is True
        assert is_phase_overdue(1, 20, target_host="unknown-host") is False

    def test_uses_learned_timing(self, tmp_path):
        timings_file = tmp_path / "phase_timings.json"
        db = {
            "fast-server:": {
                "run_count": 3,
                "phase_averages": {"1": 660, "2": 330},
            }
        }
        timings_file.write_text(json.dumps(db))

        with patch("agents.local.phase_expectations._get_phase_timings_path", return_value=timings_file):
            timing = get_server_phase_timing("fast-server", 1)
            assert timing["source"] == "learned"
            assert timing["max_minutes"] < 50
