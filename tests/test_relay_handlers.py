"""Tests for relay/handlers.py — phase detection, failure parsing, play recap, _on_line."""

import pytest

from relay.handlers import (
    _detect_phase,
    _parse_text_failures,
    _parse_play_recap,
    _extract_failures,
    _ANSI_RE,
)


# ---------------------------------------------------------------------------
# _detect_phase
# ---------------------------------------------------------------------------

class TestDetectPhase:
    """Test Ansible output phase detection from log lines."""

    def test_detects_play(self):
        assert _detect_phase("PLAY [Deploy RHEL 10 on target] ***") == "play:Deploy RHEL 10 on target"

    def test_detects_play_with_leading_whitespace(self):
        assert _detect_phase("  PLAY [Install packages] ****") == "play:Install packages"

    def test_detects_task(self):
        assert _detect_phase("TASK [sap-preconfigure : set kernel params] ****") == "task:sap-preconfigure : set kernel params"

    def test_detects_task_with_leading_whitespace(self):
        assert _detect_phase("   TASK [gather facts] ***") == "task:gather facts"

    def test_detects_play_recap(self):
        assert _detect_phase("PLAY RECAP *****") == "recap"

    def test_detects_play_recap_with_whitespace(self):
        assert _detect_phase("  PLAY RECAP ***") == "recap"

    def test_returns_empty_for_regular_output(self):
        assert _detect_phase("ok: [target-1] => {") == ""

    def test_returns_empty_for_empty_line(self):
        assert _detect_phase("") == ""

    def test_returns_empty_for_whitespace_only(self):
        assert _detect_phase("   ") == ""

    def test_play_name_truncated_at_60_chars(self):
        long_name = "A" * 100
        result = _detect_phase(f"PLAY [{long_name}] ***")
        assert result == f"play:{long_name[:60]}"

    def test_task_name_truncated_at_60_chars(self):
        long_name = "B" * 100
        result = _detect_phase(f"TASK [{long_name}] ***")
        assert result == f"task:{long_name[:60]}"

    def test_play_with_ansi_codes(self):
        # Ansible sometimes outputs ANSI color codes around PLAY/TASK markers
        line = "\x1b[0;33mPLAY [test play]\x1b[0m ***"
        result = _detect_phase(line)
        # _detect_phase checks for "PLAY [" in the stripped line
        # ANSI codes are part of the string, so PLAY [ still matches
        assert result.startswith("play:")

    def test_play_without_closing_bracket(self):
        # Malformed output without closing bracket
        result = _detect_phase("PLAY [open bracket no close")
        assert result == ""

    def test_task_without_closing_bracket(self):
        result = _detect_phase("TASK [open bracket no close")
        assert result == ""

    def test_play_takes_priority_over_task(self):
        # Both PLAY [ and TASK [ in the same line (unlikely but test precedence)
        result = _detect_phase("PLAY [contains TASK [inner]] ***")
        assert result.startswith("play:")

    def test_recap_takes_priority_over_play(self):
        # PLAY RECAP contains both "PLAY" and "RECAP"
        result = _detect_phase("PLAY RECAP ***")
        assert result == "recap"

    def test_real_ansible_play_output(self):
        line = "PLAY [Prepare system for SAP HANA] **************"
        assert _detect_phase(line) == "play:Prepare system for SAP HANA"

    def test_real_ansible_task_output(self):
        line = "TASK [sap-preconfigure : Ensure required packages are installed] *****"
        assert _detect_phase(line) == "task:sap-preconfigure : Ensure required packages are installed"

    def test_real_ansible_recap_output(self):
        line = "PLAY RECAP *********************************************************************"
        assert _detect_phase(line) == "recap"


# ---------------------------------------------------------------------------
# _parse_text_failures
# ---------------------------------------------------------------------------

class TestParseTextFailures:
    """Test extraction of failure information from Ansible text output."""

    def test_detects_fatal_error(self):
        stdout = (
            'TASK [install package]\n'
            'fatal: [target-1]: FAILED! => msg: No package matching found\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert failures[0]["task_name"] == "install package"

    def test_detects_unreachable(self):
        stdout = (
            'TASK [gather facts]\n'
            'fatal: [target-1]: UNREACHABLE! => msg: Failed to connect to the host\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert failures[0]["failure_type"] == "unreachable"

    def test_detects_failed_marker(self):
        stdout = (
            'TASK [check service]\n'
            'FAILED! => msg: service not running\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1

    def test_extracts_msg_with_colon_format(self):
        # The _MSG_RE regex matches msg: or msg= (without quotes before the delimiter)
        stdout = (
            'TASK [my task]\n'
            'fatal: [host]: FAILED! => msg: Package not found\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert "Package not found" in failures[0]["error_message"]

    def test_extracts_msg_with_equals_format(self):
        stdout = (
            'TASK [my task]\n'
            'fatal: [host]: FAILED! => msg=something went wrong\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert "something went wrong" in failures[0]["error_message"]

    def test_json_msg_falls_back_to_raw(self):
        # JSON-formatted "msg" has quotes around the key, so _MSG_RE won't match
        # and falls back to "see raw output"
        stdout = (
            'TASK [my task]\n'
            'fatal: [host]: FAILED! => {"msg": "Package not found"}\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert failures[0]["error_message"] == "see raw output"

    def test_no_failures_returns_empty(self):
        stdout = (
            'TASK [install package]\n'
            'ok: [target-1]\n'
            'TASK [start service]\n'
            'changed: [target-1]\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert failures == []

    def test_multiple_failures(self):
        stdout = (
            'TASK [task1]\n'
            'fatal: [host1]: FAILED! => {"msg": "error1"}\n'
            'TASK [task2]\n'
            'fatal: [host2]: FAILED! => {"msg": "error2"}\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) == 2
        assert failures[0]["task_name"] == "task1"
        assert failures[1]["task_name"] == "task2"

    def test_limit_to_10_failures(self):
        lines = []
        for i in range(15):
            lines.append(f'TASK [task{i}]')
            lines.append(f'fatal: [host]: FAILED! => {{"msg": "error{i}"}}')
        stdout = "\n".join(lines)
        failures = _parse_text_failures(stdout, "")
        assert len(failures) <= 10

    def test_stderr_failures_also_detected(self):
        stderr = (
            'TASK [deploy config]\n'
            'fatal: [target]: FAILED! => {"msg": "permission denied"}\n'
        )
        failures = _parse_text_failures("", stderr)
        assert len(failures) >= 1

    def test_ansi_codes_stripped(self):
        stdout = (
            'TASK [install]\n'
            '\x1b[0;31mfatal: [host]: FAILED! => msg: failed with colors\x1b[0m\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert "failed with colors" in failures[0]["error_message"]

    def test_unknown_task_when_no_task_header(self):
        stdout = 'fatal: [host]: FAILED! => {"msg": "unknown context"}\n'
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert failures[0]["task_name"] == "unknown"

    def test_see_raw_output_fallback(self):
        stdout = (
            'TASK [my task]\n'
            'fatal: [host]: FAILED! => some unparseable error\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1
        assert "see raw output" in failures[0]["error_message"]

    def test_carriage_returns_stripped(self):
        stdout = (
            'TASK [my task]\r\n'
            'fatal: [host]: FAILED! => {"msg": "error with CR"}\r\n'
        )
        failures = _parse_text_failures(stdout, "")
        assert len(failures) >= 1


# ---------------------------------------------------------------------------
# _parse_play_recap
# ---------------------------------------------------------------------------

class TestParsePlayRecap:
    """Test parsing of PLAY RECAP summary lines."""

    def test_parses_single_host(self):
        stdout = (
            'PLAY RECAP *****\n'
            'target-1.example.com : ok=50   changed=10   unreachable=0    failed=0   \n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is True
        assert "target-1.example.com" in result["hosts"]
        host = result["hosts"]["target-1.example.com"]
        assert host["ok"] == 50
        assert host["changed"] == 10
        assert host["unreachable"] == 0
        assert host["failed"] == 0

    def test_parses_multiple_hosts(self):
        stdout = (
            'PLAY RECAP *****\n'
            'host1 : ok=10  changed=5  unreachable=0  failed=0\n'
            'host2 : ok=8   changed=3  unreachable=0  failed=0\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is True
        assert len(result["hosts"]) == 2
        assert "host1" in result["hosts"]
        assert "host2" in result["hosts"]

    def test_detects_failed_host(self):
        stdout = (
            'PLAY RECAP *****\n'
            'target-1 : ok=30  changed=10  unreachable=0  failed=2\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is False
        assert result["hosts"]["target-1"]["failed"] == 2

    def test_detects_unreachable_host(self):
        stdout = (
            'PLAY RECAP *****\n'
            'target-2 : ok=0   changed=0   unreachable=1    failed=0\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is False
        assert result["hosts"]["target-2"]["unreachable"] == 1

    def test_mixed_success_and_failure(self):
        stdout = (
            'PLAY RECAP *****\n'
            'host-ok   : ok=50  changed=10  unreachable=0  failed=0\n'
            'host-fail : ok=30  changed=5   unreachable=0  failed=3\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is False
        assert result["hosts"]["host-ok"]["failed"] == 0
        assert result["hosts"]["host-fail"]["failed"] == 3

    def test_no_recap_section(self):
        stdout = "just some regular output\nno recap here\n"
        result = _parse_play_recap(stdout)
        assert result["hosts"] == {}
        assert result["all_ok"] is False

    def test_empty_stdout(self):
        result = _parse_play_recap("")
        assert result["hosts"] == {}
        assert result["all_ok"] is False

    def test_ansi_codes_in_recap(self):
        stdout = (
            '\x1b[0;33mPLAY RECAP\x1b[0m *****\n'
            '\x1b[0;32mtarget-1\x1b[0m : ok=40   changed=8   unreachable=0    failed=0\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is True
        assert len(result["hosts"]) == 1

    def test_carriage_returns_in_recap(self):
        stdout = (
            'PLAY RECAP *****\r\r\n'
            'target-1 : ok=40   changed=8   unreachable=0    failed=0\r\r\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is True
        assert len(result["hosts"]) == 1

    def test_recap_parsing_stops_at_non_recap_line(self):
        stdout = (
            'PLAY RECAP *****\n'
            'host1 : ok=10  changed=5  unreachable=0  failed=0\n'
            '\n'
            'Some other output that is not a recap line\n'
            'host-not-in-recap : ok=0  changed=0  unreachable=0  failed=0\n'
        )
        result = _parse_play_recap(stdout)
        # Empty line is skipped, non-recap line breaks the section
        assert "host1" in result["hosts"]

    def test_multiple_recap_sections(self):
        # DCI can have multiple plays, each with its own PLAY RECAP
        stdout = (
            'PLAY RECAP *****\n'
            'host1 : ok=10  changed=5  unreachable=0  failed=0\n'
            '\n'
            'PLAY RECAP *****\n'
            'host1 : ok=50  changed=10  unreachable=0  failed=1\n'
        )
        result = _parse_play_recap(stdout)
        # The second recap should overwrite the first for the same host
        assert result["hosts"]["host1"]["failed"] == 1
        assert result["all_ok"] is False

    def test_real_world_recap(self):
        stdout = (
            'PLAY RECAP *********************************************************************\n'
            'target-1.example.corp   : ok=499   changed=123  unreachable=0    failed=0    skipped=42    rescued=0    ignored=3\n'
        )
        result = _parse_play_recap(stdout)
        assert result["all_ok"] is True
        host = result["hosts"]["target-1.example.corp"]
        assert host["ok"] == 499
        assert host["changed"] == 123


# ---------------------------------------------------------------------------
# _extract_failures
# ---------------------------------------------------------------------------

class TestExtractFailures:
    """Test the top-level failure extraction dispatcher."""

    def test_text_failures_from_ansible_output(self):
        stdout = (
            'TASK [my task]\n'
            'fatal: [host]: FAILED! => msg: package not found\n'
        )
        failures = _extract_failures(stdout, "")
        assert len(failures) >= 1
        assert "package not found" in failures[0]["error_message"]

    def test_json_failures_from_structured_output(self):
        data = {
            "plays": [{
                "tasks": [{
                    "task": {"name": "install sap", "path": "/hooks/main.yml", "module": "yum"},
                    "hosts": {
                        "target-1": {
                            "failed": True,
                            "msg": "No package sap-hana available",
                        }
                    }
                }]
            }]
        }
        stdout = json.dumps(data)
        failures = _extract_failures(stdout, "")
        assert len(failures) == 1
        assert failures[0]["task_name"] == "install sap"
        assert failures[0]["host"] == "target-1"
        assert "sap-hana" in failures[0]["error_message"]

    def test_empty_stdout_returns_empty(self):
        failures = _extract_failures("", "")
        assert failures == []

    def test_json_with_no_failures(self):
        data = {
            "plays": [{
                "tasks": [{
                    "task": {"name": "ok task", "path": "/hooks/main.yml", "module": "yum"},
                    "hosts": {
                        "target-1": {"failed": False, "changed": True}
                    }
                }]
            }]
        }
        stdout = json.dumps(data)
        failures = _extract_failures(stdout, "")
        assert failures == []

    def test_json_unreachable_host(self):
        data = {
            "plays": [{
                "tasks": [{
                    "task": {"name": "gather facts", "path": "/test.yml", "module": "setup"},
                    "hosts": {
                        "target-1": {
                            "unreachable": True,
                            "msg": "Failed to connect via SSH",
                        }
                    }
                }]
            }]
        }
        stdout = json.dumps(data)
        failures = _extract_failures(stdout, "")
        assert len(failures) == 1
        assert "SSH" in failures[0]["error_message"]

    def test_invalid_json_falls_back_to_text_parsing(self):
        stdout = '{invalid json\nTASK [my task]\nfatal: [host]: FAILED! => {"msg": "err"}\n'
        failures = _extract_failures(stdout, "")
        assert len(failures) >= 1

    def test_json_array_falls_back_to_text(self):
        # JSON array at top level — not the expected dict format
        stdout = '["not", "a", "dict"]'
        failures = _extract_failures(stdout, "")
        # Falls through to text parsing since it's not a dict
        assert isinstance(failures, list)


# ---------------------------------------------------------------------------
# _on_line failure marker detection
# ---------------------------------------------------------------------------

class TestOnLineFailureMarkers:
    """Test the _FAILURE_MARKERS detection logic used in _on_line."""

    # These are the markers defined inline in _run_workflow
    _FAILURE_MARKERS = ("fatal:", "FAILED!", "UNREACHABLE!")

    @pytest.mark.parametrize("line,expected", [
        ('fatal: [host]: FAILED! => {"msg": "error"}', True),
        ('FAILED! => {"msg": "something"}', True),
        ('UNREACHABLE! => {"msg": "no route"}', True),
        ('ok: [host]', False),
        ('changed: [host]', False),
        ('skipping: [host]', False),
        ('TASK [some task] ***', False),
        ('PLAY RECAP ***', False),
    ])
    def test_marker_detection(self, line, expected):
        has_marker = any(marker in line for marker in self._FAILURE_MARKERS)
        assert has_marker is expected

    def test_fatal_with_ansi_codes(self):
        line = '\x1b[0;31mfatal: [host]: FAILED!\x1b[0m'
        has_marker = any(marker in line for marker in self._FAILURE_MARKERS)
        assert has_marker is True

    def test_case_sensitive(self):
        # Ansible markers are case-sensitive
        assert not any(m in "Fatal: error" for m in self._FAILURE_MARKERS)
        assert not any(m in "failed! error" for m in self._FAILURE_MARKERS)
        assert not any(m in "unreachable! error" for m in self._FAILURE_MARKERS)


# ---------------------------------------------------------------------------
# ANSI regex stripping
# ---------------------------------------------------------------------------

class TestAnsiRegex:
    """Test the ANSI escape code stripping regex used in parsing."""

    def test_strips_color_codes(self):
        text = "\x1b[0;31mERROR\x1b[0m"
        assert _ANSI_RE.sub("", text) == "ERROR"

    def test_strips_bold(self):
        text = "\x1b[1mBOLD\x1b[0m"
        assert _ANSI_RE.sub("", text) == "BOLD"

    def test_strips_multiple_codes(self):
        text = "\x1b[0;31m\x1b[1mRED BOLD\x1b[0m\x1b[0m"
        assert _ANSI_RE.sub("", text) == "RED BOLD"

    def test_preserves_plain_text(self):
        text = "no colors here"
        assert _ANSI_RE.sub("", text) == text

    def test_strips_from_real_ansible_output(self):
        text = "\x1b[0;31mfatal: [target-1]: FAILED! => {\"msg\": \"No package\"}\x1b[0m"
        clean = _ANSI_RE.sub("", text)
        assert "fatal:" in clean
        assert "FAILED!" in clean
        assert "\x1b" not in clean


# ---------------------------------------------------------------------------
# _is_git_url
# ---------------------------------------------------------------------------

class TestIsGitUrl:
    """Test the git URL detection helper."""

    def test_https_url(self):
        from relay.handlers import _is_git_url
        assert _is_git_url("https://github.com/org/repo.git") is True

    def test_git_ssh_url(self):
        from relay.handlers import _is_git_url
        assert _is_git_url("git@github.com:org/repo.git") is True

    def test_local_path(self):
        from relay.handlers import _is_git_url
        assert _is_git_url("/agentic-dci-workflow/dci-hooks") is False

    def test_empty_string(self):
        from relay.handlers import _is_git_url
        assert _is_git_url("") is False


# ---------------------------------------------------------------------------
# Integration: _parse_play_recap + _extract_failures together
# ---------------------------------------------------------------------------

class TestRecapAndFailuresIntegration:
    """Test that recap and failure extraction work together on realistic output."""

    def test_successful_run(self):
        stdout = (
            'PLAY [Deploy RHEL] ***\n'
            'TASK [install] ***\n'
            'ok: [target-1]\n'
            'TASK [configure] ***\n'
            'changed: [target-1]\n'
            'PLAY RECAP *****\n'
            'target-1 : ok=10  changed=5  unreachable=0  failed=0\n'
        )
        recap = _parse_play_recap(stdout)
        failures = _extract_failures(stdout, "")
        assert recap["all_ok"] is True
        assert failures == []

    def test_failed_run(self):
        stdout = (
            'PLAY [Deploy RHEL] ***\n'
            'TASK [install package] ***\n'
            'fatal: [target-1]: FAILED! => msg: Package not found\n'
            'PLAY RECAP *****\n'
            'target-1 : ok=5  changed=2  unreachable=0  failed=1\n'
        )
        recap = _parse_play_recap(stdout)
        failures = _extract_failures(stdout, "")
        assert recap["all_ok"] is False
        assert recap["hosts"]["target-1"]["failed"] == 1
        assert len(failures) >= 1
        assert "Package not found" in failures[0]["error_message"]

    def test_unreachable_run(self):
        stdout = (
            'PLAY [Deploy RHEL] ***\n'
            'TASK [gather facts] ***\n'
            'fatal: [target-1]: UNREACHABLE! => {"msg": "SSH connection timeout"}\n'
            'PLAY RECAP *****\n'
            'target-1 : ok=0  changed=0  unreachable=1  failed=0\n'
        )
        recap = _parse_play_recap(stdout)
        failures = _extract_failures(stdout, "")
        assert recap["all_ok"] is False
        assert recap["hosts"]["target-1"]["unreachable"] == 1
        assert len(failures) >= 1
        assert failures[0]["failure_type"] == "unreachable"


# We need json for _extract_failures JSON tests
import json
