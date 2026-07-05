"""
Phase expectations — what the world should look like after each DCI phase.

Provides a phase validator (inspired by world model concepts) that lets the agent distinguish normal
from abnormal server state. Each phase has expected post-conditions and
typical timing. check_phase_expectations() compares actual state to the
model and returns deviations worth investigating.

Timing baselines are learned per-server from run_journal history when
available, falling back to static defaults for unknown servers.
"""

import json
import logging
import re
import statistics
from pathlib import Path

logger = logging.getLogger(__name__)

PHASE_EXPECTATIONS = {
    1: {
        "name": "OS Deployment",
        "expected_state": {
            "ssh_accessible": True,
            "os_installed": True,
            "repos_enabled": True,
            "correct_rhel_version": True,
        },
        "typical_duration_minutes": 30,
        "max_duration_minutes": 50,
        "critical_services": ["sshd"],
        "description": "Fresh RHEL installed via kickstart, SSH reachable, repos configured.",
    },
    2: {
        "name": "OS Prep for HANA",
        "expected_state": {
            "tuned_profile": "sap-hana",
            "kernel_params_set": True,
            "selinux_mode": "permissive",
            "sap_packages_installed": True,
            "hana_filesystems_mounted": True,
        },
        "typical_duration_minutes": 15,
        "max_duration_minutes": 30,
        "critical_services": ["tuned"],
        "description": "SAP preconfigure roles applied: tuned profile, kernel params, storage.",
    },
    3: {
        "name": "HANA Installation",
        "expected_state": {
            "hana_installed": True,
            "hana_running": True,
            "sidadm_user_exists": True,
        },
        "typical_duration_minutes": 20,
        "max_duration_minutes": 40,
        "critical_services": ["sapstartsrv"],
        "description": "SAP HANA installed via hdblcm, database instance started.",
    },
    4: {
        "name": "PBO Install and Run",
        "expected_state": {
            "pbo_installed": True,
            "pbo_completed": True,
        },
        "typical_duration_minutes": 65,
        "max_duration_minutes": 90,
        "critical_services": ["sapstartsrv"],
        "description": "PBOffline benchmark installed and executed.",
    },
    5: {
        "name": "Results",
        "expected_state": {
            "results_collected": True,
            "results_uploaded": True,
        },
        "typical_duration_minutes": 5,
        "max_duration_minutes": 15,
        "critical_services": [],
        "description": "Benchmark results collected and uploaded to DCI backend.",
    },
}


PHASE_PATTERNS: dict[int, list[str]] = {
    1: [r"deploy", r"kickstart", r"pxe", r"install.*rhel", r"os.*deploy",
        r"provision", r"bare.?metal"],
    2: [r"sap.preconfigure", r"sap.hana.preconfigure", r"tuned", r"kernel.*param",
        r"prep.*hana", r"os.*prep", r"sap.*prep"],
    3: [r"hdblcm", r"hana.*install", r"install.*hana", r"saphana",
        r"hana.*setup", r"database.*install"],
    4: [r"pboffline", r"pbo", r"benchmark", r"perf.*bench"],
    5: [r"result", r"report", r"upload", r"junit", r"collect.*result"],
}

_COMPILED_PATTERNS: dict[int, list[re.Pattern]] = {
    phase: [re.compile(p, re.IGNORECASE) for p in patterns]
    for phase, patterns in PHASE_PATTERNS.items()
}


def detect_phase_number(ansible_phase_string: str) -> int | None:
    """Map a relay phase string to a phase number 1-5.

    The relay's _detect_phase() produces strings like "play:OS Deployment"
    or "task:sap-preconfigure : Ensure required packages". This function
    matches those against known patterns for each phase.

    Returns None if no phase matches.
    """
    if not ansible_phase_string:
        return None
    text = ansible_phase_string.lower()
    for phase, patterns in _COMPILED_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(text):
                return phase
    return None


def _get_journal_path() -> Path:
    log_dir = Path("/tmp/dci-agent-logs")
    try:
        from .. import config
        log_dir = config.LOG_DIR
    except Exception:
        pass
    return log_dir / "run_journal.jsonl"


def _get_phase_timings_path() -> Path:
    """Path to the phase timings file written by the workflow poller."""
    log_dir = Path("/tmp/dci-agent-logs")
    try:
        from .. import config
        log_dir = config.LOG_DIR
    except Exception:
        pass
    return log_dir / "phase_timings.json"


def _load_phase_timings_for_server(
    target_host: str,
    rhel_topic: str = "",
) -> dict[int, list[float]]:
    """Load averaged per-phase durations from phase_timings.json.

    The file stores running averages keyed by "hostname:topic":
    {"target-1:RHEL-10.2": {"run_count": 5, "phase_averages": {"1": 1800, ...}}}

    Returns a dict mapping phase_num → [average_minutes] (single-element
    list for compatibility with get_server_phase_timing's median/p90 logic).
    The run_count is used as the data_points count.
    """
    timings_path = _get_phase_timings_path()
    if not timings_path.exists():
        return {}

    short_host = target_host.split(".")[0]
    key = f"{short_host}:{rhel_topic}" if rhel_topic else None

    try:
        db = json.loads(timings_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    if key and key in db:
        entry = db[key]
    else:
        for k, v in db.items():
            if k.startswith(f"{short_host}:"):
                entry = v
                break
        else:
            return {}

    timings: dict[int, list[float]] = {}
    run_count = entry.get("run_count", 0)
    for phase_str, avg_secs in entry.get("phase_averages", {}).items():
        try:
            phase_num = int(phase_str)
            if avg_secs > 0:
                timings[phase_num] = [avg_secs / 60.0] * run_count
        except (ValueError, TypeError):
            continue

    return timings


def get_server_phase_timing(
    target_host: str,
    phase: int,
    rhel_topic: str = "",
    min_data_points: int = 3,
) -> dict:
    """Return learned timing for a specific server and phase.

    Falls back to static PHASE_EXPECTATIONS defaults when fewer than
    min_data_points historical runs exist.
    """
    static = get_phase_timing(phase)
    if "error" in static:
        return static

    timings = _load_phase_timings_for_server(target_host, rhel_topic)
    phase_times = timings.get(phase, [])

    if len(phase_times) < min_data_points:
        static["source"] = "static_default"
        static["data_points"] = len(phase_times)
        return static

    sorted_times = sorted(phase_times)
    typical = statistics.median(sorted_times)
    p90_idx = int(len(sorted_times) * 0.9)
    max_learned = sorted_times[min(p90_idx, len(sorted_times) - 1)]

    return {
        "phase": phase,
        "name": static["name"],
        "typical_minutes": round(typical, 1),
        "max_minutes": round(max_learned, 1),
        "source": "learned",
        "data_points": len(phase_times),
    }


def check_phase_expectations(phase: int, actual_state: dict) -> dict:
    """Compare actual server state to phase expectations.

    Args:
        phase: DCI phase number (1-5).
        actual_state: Dict of observed state values, keyed to match
                      expected_state keys (e.g. {"tuned_profile": "sap-hana",
                      "selinux_mode": "enforcing"}).

    Returns:
        Dict with:
        - phase_name: human-readable phase name
        - met: list of expectations that match
        - deviations: list of dicts describing mismatches
        - missing: expectations not checkable (no actual value provided)
    """
    expectations = PHASE_EXPECTATIONS.get(phase)
    if not expectations:
        return {"error": f"Unknown phase: {phase}"}

    expected = expectations["expected_state"]
    met = []
    deviations = []
    missing = []

    for key, expected_value in expected.items():
        if key not in actual_state:
            missing.append(key)
            continue

        actual_value = actual_state[key]

        if actual_value == expected_value:
            met.append(key)
        else:
            deviations.append({
                "check": key,
                "expected": expected_value,
                "actual": actual_value,
            })

    return {
        "phase": phase,
        "phase_name": expectations["name"],
        "met": met,
        "deviations": deviations,
        "missing": missing,
        "all_met": len(deviations) == 0 and len(missing) == 0,
    }


def get_phase_timing(phase: int, target_host: str = "") -> dict:
    """Return timing expectations for a phase.

    When target_host is provided, returns learned per-server baselines
    from journal history. Falls back to static defaults for unknown servers
    or when insufficient data exists.
    """
    if target_host:
        return get_server_phase_timing(target_host, phase)
    expectations = PHASE_EXPECTATIONS.get(phase)
    if not expectations:
        return {"error": f"Unknown phase: {phase}"}
    return {
        "phase": phase,
        "name": expectations["name"],
        "typical_minutes": expectations["typical_duration_minutes"],
        "max_minutes": expectations["max_duration_minutes"],
        "source": "static_default",
        "data_points": 0,
    }


def is_phase_overdue(phase: int, elapsed_minutes: float, target_host: str = "") -> bool:
    """Check if a phase has exceeded its max expected duration.

    When target_host is provided, uses learned per-server timing.
    """
    timing = get_phase_timing(phase, target_host)
    if "error" in timing:
        return False
    return elapsed_minutes > timing["max_minutes"]


def format_phase_report(
    phase: int,
    actual_state: dict,
    elapsed_minutes: float = 0,
    target_host: str = "",
) -> str:
    """Human-readable report comparing actual state to expectations."""
    result = check_phase_expectations(phase, actual_state)
    if "error" in result:
        return result["error"]

    lines = [f"Phase {phase} ({result['phase_name']}) Status:"]

    if result["all_met"]:
        lines.append("  All expectations met.")
    else:
        if result["met"]:
            lines.append(f"  Met: {', '.join(result['met'])}")
        if result["deviations"]:
            lines.append("  Deviations:")
            for d in result["deviations"]:
                lines.append(f"    {d['check']}: expected={d['expected']}, actual={d['actual']}")
        if result["missing"]:
            lines.append(f"  Not checked: {', '.join(result['missing'])}")

    if elapsed_minutes:
        timing = get_phase_timing(phase, target_host)
        overdue = is_phase_overdue(phase, elapsed_minutes, target_host)
        status = "OVERDUE" if overdue else "on track"
        source_tag = f" [{timing.get('source', 'static')}]" if timing.get("source") == "learned" else ""
        lines.append(
            f"  Timing: {elapsed_minutes:.0f}min elapsed "
            f"(typical: {timing['typical_minutes']}min, max: {timing['max_minutes']}min) "
            f"— {status}{source_tag}"
        )

    return "\n".join(lines)
