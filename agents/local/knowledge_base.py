"""
Persistent knowledge store of past diagnoses and fixes.

Accumulates operational history so the agent can learn from previous runs.
Stored as a JSON file that grows over time.

Features:
- Semantic search via sentence-transformers embeddings
- Failure taxonomy with 10 categories
- Split metrics: agent vs human success rates
- Server state capture for context-aware retrieval
- Human fix auto-detection from git history
"""

import json
import datetime
import logging
import subprocess
import re
from pathlib import Path

import numpy as np

from .. import config

logger = logging.getLogger(__name__)

_KB_PATH = config.LOG_DIR / "knowledge_base.json"

# [AGENT-DISABLED] Per-subagent KB paths — replaced by single shared KB with domain tags
# _SCOPED_KB_PATHS = {
#     "hana-expert": config.LOG_DIR / "hana_expert_kb.json",
#     "dci-diagnostician": config.LOG_DIR / "diagnostician_kb.json",
#     "os-deploy-expert": config.LOG_DIR / "os_deploy_kb.json",
# }


def _kb_path_for(scope: str = "") -> Path:
    """All scopes share the main KB file. Scope is stored as a tag, not a file."""
    return _KB_PATH

# ---------------------------------------------------------------------------
# Embedding model — lazy-loaded on first use
# ---------------------------------------------------------------------------

_MODEL_NAME = "all-MiniLM-L6-v2"
_model = None


def is_model_cached() -> bool:
    """Check if the embedding model is already downloaded."""
    try:
        from huggingface_hub import try_to_load_from_cache
        result = try_to_load_from_cache(f"sentence-transformers/{_MODEL_NAME}", "config.json")
        return result is not None
    except Exception:
        try:
            from pathlib import Path
            cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
            return any(p.name.startswith("models--sentence-transformers") and _MODEL_NAME in p.name
                       for p in cache_dir.iterdir()) if cache_dir.exists() else False
        except Exception:
            return False


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _embed(text: str) -> list[float]:
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr = np.array(a)
    b_arr = np.array(b)
    return float(np.dot(a_arr, b_arr))


# ---------------------------------------------------------------------------
# Failure taxonomy — keyword-based classification
# ---------------------------------------------------------------------------

FAILURE_CATEGORIES = {
    "package_resolution": [
        "not available", "no match for argument", "nothing provides",
        "package not found", "no candidate", "cannot install",
        "depsolve", "dependency", "obsoleted",
    ],
    "selinux": [
        "avc", "selinux", "denied", "sealert", "sebool",
        "restorecon", "semanage", "enforcing",
    ],
    "tuned_profile": [
        "tuned", "tuned-adm", "tuned-profiles", "sap-hana profile",
        "profile not found", "no active profile",
    ],
    "role_version": [
        "role version", "role not found", "galaxy", "ansible-galaxy",
        "role mismatch", "collection version", "requirements.yml",
        "sap-hana-preconfigure", "sap-netweaver-preconfigure",
        "sap_preconfigure", "role_path",
    ],
    "storage_layout": [
        "lvm", "vg ", "lv ", "pv ", "multipath", "disk",
        "mount", "fstab", "filesystem", "no space", "xfs",
        "/hana/data", "/hana/log", "/hana/shared",
    ],
    "service_startup": [
        "systemctl", "service failed", "inactive (dead)",
        "start request repeated", "sapstartsrv", "saphostagent",
        "nameserver", "indexserver", "timeout waiting",
    ],
    "kernel_parameter": [
        "sysctl", "vm.swappiness", "transparent_hugepage",
        "kernel.sem", "net.core", "kernel parameter",
    ],
    "network": [
        "unreachable", "connection refused", "timeout",
        "name resolution", "dns", "no route", "ssh",
    ],
    "sap_hana_install": [
        "hdbinst", "hdblcm", "hana install", "sid ",
        "sapcontrol", "hdb ", "hdbadm", "sidadm",
        "license", "hana media",
    ],
    "upstream_bug": [
        "known issue", "bug", "workaround", "bz#", "rhbz",
        "errata", "advisory", "regression",
    ],
}


def classify_failure(error_pattern: str, diagnosis: str = "") -> str:
    """Classify a failure into one of the taxonomy categories.

    Returns the category name, or "uncategorized" if no match.
    """
    text = f"{error_pattern} {diagnosis}".lower()

    scores: dict[str, int] = {}
    for category, keywords in FAILURE_CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[category] = score

    if not scores:
        return "uncategorized"

    return max(scores, key=scores.get)


# ---------------------------------------------------------------------------
# Fix pattern taxonomy (compositional decomposition)
# ---------------------------------------------------------------------------

FIX_PATTERNS = {
    "add_missing_package",
    "disable_broken_task",
    "fix_variable_value",
    "add_when_condition",
    "update_role_version",
    "fix_storage_layout",
    "fix_selinux_context",
    "fix_tuned_profile",
    "add_prerequisite_task",
    "fix_file_permissions",
    "workaround_upstream_bug",
    "custom",
}

_FIX_PATTERN_KEYWORDS = {
    "add_missing_package": ["package", "install", "yum", "dnf", "rpm", "missing"],
    "disable_broken_task": ["disable", "comment out", "skip", "agent-disabled"],
    "fix_variable_value": ["variable", "value", "default", "set to", "changed from"],
    "add_when_condition": ["when:", "condition", "skip when", "only_if"],
    "update_role_version": ["role", "galaxy", "version", "collection", "requirements"],
    "fix_storage_layout": ["disk", "lvm", "mount", "partition", "ondisk", "storage"],
    "fix_selinux_context": ["selinux", "context", "restorecon", "setype", "avc"],
    "fix_tuned_profile": ["tuned", "profile", "sap-hana", "tuned-adm"],
    "add_prerequisite_task": ["prerequisite", "before", "depends", "pre-task"],
    "fix_file_permissions": ["permission", "chmod", "chown", "mode", "executable"],
    "workaround_upstream_bug": ["workaround", "bug", "upstream", "known issue"],
}


def classify_fix_pattern(fix_applied: str, diagnosis: str = "") -> str:
    """Classify a fix into a reusable pattern category."""
    text = f"{fix_applied} {diagnosis}".lower()
    scores: dict[str, int] = {}
    for pattern, keywords in _FIX_PATTERN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[pattern] = score
    if not scores:
        return "custom"
    return max(scores, key=scores.get)


# ---------------------------------------------------------------------------
# Server state capture
# ---------------------------------------------------------------------------

def capture_server_state(diagnostics_output: str) -> dict:
    """Extract structured server state from diagnostics output.

    Parses the text output from dci_ssh_diagnostics or manual SSH commands
    into a structured dict for storage alongside KB entries.
    """
    state = {}

    rhel_match = re.search(r"Red Hat Enterprise Linux.*?(\d+\.\d+)", diagnostics_output)
    if rhel_match:
        state["rhel_version"] = rhel_match.group(1)

    kernel_match = re.search(r"(\d+\.\d+\.\d+-[\d.]+\.el\d+[^\s]*)", diagnostics_output)
    if kernel_match:
        state["kernel"] = kernel_match.group(1)

    for mode in ("enforcing", "permissive", "disabled"):
        if mode in diagnostics_output.lower():
            state["selinux"] = mode
            break

    tuned_match = re.search(r"(?:current active profile|active profile):\s*(.+)", diagnostics_output, re.IGNORECASE)
    if tuned_match:
        state["tuned_profile"] = tuned_match.group(1).strip()

    mem_match = re.search(r"MemTotal:\s+(\d+)\s+kB", diagnostics_output)
    if mem_match:
        state["memory_gb"] = round(int(mem_match.group(1)) / 1024 / 1024, 1)

    return state


# ---------------------------------------------------------------------------
# Server profiles — persistent per-host state snapshots
# ---------------------------------------------------------------------------

_PROFILES_PATH = config.LOG_DIR / "server_profiles.json"


def _load_profiles() -> dict:
    if _PROFILES_PATH.exists():
        try:
            return json.loads(_PROFILES_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_profiles(profiles: dict):
    from .filelock import atomic_write_json
    atomic_write_json(_PROFILES_PATH, profiles)


def save_server_profile(target_host: str, state: dict,
                        run_result: dict | None = None) -> dict:
    """Save or update the server profile for a target host.

    Args:
        target_host: FQDN of the target server.
        state: Structured state dict from capture_server_state().
        run_result: Optional dict with last run outcome:
                    {success, phase_reached, tasks_passed, rhel_topic}.
    """
    profiles = _load_profiles()

    existing = profiles.get(target_host, {})
    existing.update(state)
    existing["last_updated"] = datetime.datetime.now().isoformat()

    if run_result:
        existing["last_run"] = {
            "timestamp": datetime.datetime.now().isoformat(),
            "success": run_result.get("success", False),
            "phase_reached": run_result.get("phase_reached", 0),
            "tasks_passed": run_result.get("tasks_passed", 0),
            "rhel_topic": run_result.get("rhel_topic", ""),
        }

    profiles[target_host] = existing
    _save_profiles(profiles)

    return {"success": True, "host": target_host, "profile": existing}


def get_server_profile(target_host: str) -> dict:
    """Get the last-known profile for a target server.

    Returns the profile dict, or an empty dict with a message if unknown.
    """
    profiles = _load_profiles()
    profile = profiles.get(target_host)
    if profile:
        return {"host": target_host, "profile": profile}
    return {"host": target_host, "profile": None,
            "message": f"No prior profile for {target_host} — first run on this server."}


def get_all_server_profiles() -> dict:
    """Return all known server profiles."""
    return _load_profiles()


# ---------------------------------------------------------------------------
# Persistence — knowledge base
# ---------------------------------------------------------------------------

def _load(kb_scope: str = "") -> list[dict]:
    path = _kb_path_for(kb_scope)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Corrupted %s, returning empty: %s", path.name, e)
            return []
    return []


def _save(entries: list[dict], kb_scope: str = ""):
    from .filelock import atomic_write_json
    atomic_write_json(_kb_path_for(kb_scope), entries)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def record_fix(error_pattern: str, diagnosis: str, fix_applied: str,
               files_changed: list[str], success: bool,
               target_host: str = "", rhel_version: str = "",
               server_state: dict | None = None,
               phase_reached: int = 0, tasks_passed: int = 0,
               attempt_number: int = 0, source: str = "agent",
               commit_sha: str = "", run_id: str = "",
               fix_pattern: str = "", kb_scope: str = "") -> dict:
    """Record a diagnosis and fix attempt for future reference.

    Args:
        error_pattern: The key error message or pattern that identified the failure.
        diagnosis: What was found during investigation.
        fix_applied: What change was made.
        files_changed: Which files were modified.
        success: Whether the fix resolved the issue.
        target_host: The target server.
        rhel_version: The RHEL version being tested.
        server_state: Structured server state dict from capture_server_state().
        phase_reached: Highest workflow phase reached (1-4).
        tasks_passed: Number of Ansible tasks that passed before failure.
        attempt_number: Which fix attempt this was (1-5).
        source: "agent" or "human".
        commit_sha: Git commit SHA for deduplication.
        kb_scope: Domain tag — "hana-expert", "dci-diagnostician",
                  "os-deploy-expert", or "" for general. Stored on the entry,
                  not used for file selection.
    """
    entries = _load()

    if commit_sha:
        for existing in entries:
            if existing.get("commit_sha") == commit_sha:
                return {"success": True, "message": "Commit already in knowledge base.", "total_entries": len(entries)}

    failure_category = classify_failure(error_pattern, diagnosis)
    if not fix_pattern:
        fix_pattern = classify_fix_pattern(fix_applied, diagnosis)

    searchable_text = f"{error_pattern} {diagnosis} {fix_applied}"
    try:
        embedding = _embed(searchable_text)
    except Exception as e:
        logger.warning("Embedding failed, storing without: %s", e)
        embedding = []

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "error_pattern": error_pattern,
        "diagnosis": diagnosis,
        "fix_applied": fix_applied,
        "files_changed": files_changed,
        "success": success,
        "target_host": target_host or config.TARGET_HOST,
        "rhel_version": rhel_version,
        "failure_category": failure_category,
        "fix_pattern": fix_pattern,
        "source": source,
        "server_state": server_state or {},
        "outcome": {
            "phase_reached": phase_reached,
            "tasks_passed": tasks_passed,
            "attempt_number": attempt_number,
        },
        "commit_sha": commit_sha,
        "run_id": run_id,
        "domain": kb_scope,
        "_embedding": embedding,
    }
    entries.append(entry)
    _save(entries)

    try:
        from .events import emit as unified_emit, normalize_error, error_signature
        unified_emit(
            "kb.fix_recorded",
            run_id=run_id,
            target_host=target_host,
            rhel_topic=rhel_version,
            attempt_number=attempt_number,
            phase=phase_reached,
            data={"success": success, "error_pattern": error_pattern,
                  "diagnosis": diagnosis, "fix_applied": fix_applied,
                  "source": source, "commit_sha": commit_sha},
            root_cause_category=failure_category,
            fix_pattern=fix_pattern,
            normalized_error=normalize_error(error_pattern),
            error_sig=error_signature(error_pattern),
            environment_context=server_state or {},
        )
    except Exception as e:
        logger.warning("Unified event forward failed in record_fix: %s", e)

    return {"success": True, "message": f"Fix recorded ({failure_category}).", "total_entries": len(entries)}


def search_knowledge(error_pattern: str, threshold: float = 0.5,
                     max_results: int = 10, kb_scope: str = "",
                     phase: int = 0) -> dict:
    """Search the shared knowledge base using semantic similarity.

    Args:
        error_pattern: Text to search for — matched against error patterns,
                       diagnoses, and fixes using embedding similarity.
        threshold: Minimum cosine similarity to include (0.0 to 1.0).
        max_results: Maximum number of results to return.
        kb_scope: Filter by domain tag (e.g. "os-deploy-expert"). Empty = all.
        phase: Filter by phase_reached (1-5). 0 = all phases.
    """
    entries = _load()
    if kb_scope:
        entries = [e for e in entries if e.get("domain") == kb_scope]
    if phase:
        entries = [e for e in entries
                   if e.get("outcome", {}).get("phase_reached") == phase]
    if not entries:
        return {"query": error_pattern, "match_count": 0, "matches": []}

    try:
        query_embedding = _embed(error_pattern)
    except Exception:
        logger.warning("Embedding failed, falling back to substring search")
        return _substring_search(entries, error_pattern, max_results)

    scored = []
    for entry in entries:
        entry_embedding = entry.get("_embedding", [])
        if not entry_embedding:
            searchable = f"{entry.get('error_pattern', '')} {entry.get('diagnosis', '')}".lower()
            if error_pattern.lower() in searchable:
                scored.append((0.5, entry))
            continue

        similarity = _cosine_similarity(query_embedding, entry_embedding)
        if similarity >= threshold:
            scored.append((similarity, entry))

    scored.sort(key=lambda x: (-x[1].get("success", False), -x[0]))

    matches = []
    for similarity, entry in scored[:max_results]:
        result = {k: v for k, v in entry.items() if k != "_embedding"}
        result["_similarity"] = round(similarity, 3)
        matches.append(result)

    return {
        "query": error_pattern,
        "match_count": len(matches),
        "matches": matches,
    }


def _substring_search(entries: list[dict], pattern: str, max_results: int) -> dict:
    """Fallback when embedding model is unavailable."""
    pattern_lower = pattern.lower()
    matches = []
    for entry in entries:
        searchable = f"{entry.get('error_pattern', '')} {entry.get('diagnosis', '')}".lower()
        if pattern_lower in searchable:
            result = {k: v for k, v in entry.items() if k != "_embedding"}
            result["_similarity"] = 0.5
            matches.append(result)

    matches.sort(key=lambda e: (not e.get("success", False), e.get("timestamp", "")), reverse=True)

    return {
        "query": pattern,
        "match_count": len(matches[:max_results]),
        "matches": matches[:max_results],
    }


# ---------------------------------------------------------------------------
# Category statistics with split metrics
# ---------------------------------------------------------------------------

def get_category_stats(kb_scope: str = "", phase: int = 0) -> dict:
    """Compute per-category success rates split by source (agent vs human).

    Args:
        kb_scope: Filter by domain tag. Empty = all.
        phase: Filter by phase_reached (1-5). 0 = all.

    Returns a dict keyed by failure_category, each containing:
    - total: total entries in this category
    - agent_total / agent_success / agent_success_rate
    - human_total / human_success / human_success_rate
    - overall_success_rate
    - human_intervention_rate: fraction of entries that came from humans
    """
    entries = _load()
    if kb_scope:
        entries = [e for e in entries if e.get("domain") == kb_scope]
    if phase:
        entries = [e for e in entries
                   if e.get("outcome", {}).get("phase_reached") == phase]
    stats: dict[str, dict] = {}

    for entry in entries:
        cat = entry.get("failure_category", "uncategorized")
        source = entry.get("source", "agent")
        success = entry.get("success", False)

        if cat not in stats:
            stats[cat] = {
                "total": 0,
                "agent_total": 0, "agent_success": 0,
                "human_total": 0, "human_success": 0,
            }

        s = stats[cat]
        s["total"] += 1

        if source == "human":
            s["human_total"] += 1
            if success:
                s["human_success"] += 1
        else:
            s["agent_total"] += 1
            if success:
                s["agent_success"] += 1

    for cat, s in stats.items():
        s["agent_success_rate"] = (
            round(s["agent_success"] / s["agent_total"], 2)
            if s["agent_total"] > 0 else None
        )
        s["human_success_rate"] = (
            round(s["human_success"] / s["human_total"], 2)
            if s["human_total"] > 0 else None
        )
        s["overall_success_rate"] = (
            round((s["agent_success"] + s["human_success"]) / s["total"], 2)
            if s["total"] > 0 else None
        )
        s["human_intervention_rate"] = (
            round(s["human_total"] / s["total"], 2)
            if s["total"] > 0 else 0
        )

    return stats


# ---------------------------------------------------------------------------
# Summary for system prompt
# ---------------------------------------------------------------------------

def get_knowledge_summary() -> str:
    """Return a summary of the knowledge base for inclusion in the system prompt.

    Includes category stats with agent vs overall success rates.
    """
    entries = _load()
    if not entries:
        return "No past fixes recorded yet."

    successful = [e for e in entries if e.get("success")]
    failed = [e for e in entries if not e.get("success")]

    lines = [f"Knowledge base: {len(entries)} entries ({len(successful)} successful, {len(failed)} failed)."]
    lines.append("")

    stats = get_category_stats()
    if stats:
        lines.append("Category stats (agent_rate / overall_rate):")
        for cat, s in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True):
            agent_rate = f"{s['agent_success_rate']:.0%}" if s["agent_success_rate"] is not None else "n/a"
            overall_rate = f"{s['overall_success_rate']:.0%}" if s["overall_success_rate"] is not None else "n/a"
            lines.append(f"  {cat}: {s['total']} entries, agent {agent_rate}, overall {overall_rate}")
        lines.append("")

    recent = successful[-5:] if successful else entries[-5:]
    for e in reversed(recent):
        status = "SUCCESS" if e.get("success") else "FAILED"
        cat = e.get("failure_category", "?")
        lines.append(f"- [{status}] [{cat}] {e.get('error_pattern', '?')[:80]}")
        lines.append(f"  Fix: {e.get('fix_applied', '?')[:80]}")
        lines.append(f"  Date: {e.get('timestamp', '?')[:10]}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Human fix auto-detection from git history
# ---------------------------------------------------------------------------

AGENT_COMMIT_PATTERN = re.compile(r"\[agent-fix attempt \d+\]")
AGENT_COMMITTER = "DCI Agent"


def ingest_human_fixes(repo_root: str = "") -> dict:
    """Scan git history for human fixes to hooks and ingest into KB.

    Looks for commits touching dci-hooks_*/ that were NOT made by the agent.
    Creates KB entries with source="human" for each.

    Args:
        repo_root: Path to the git repo root. Defaults to CWD.
    """
    if not repo_root:
        repo_root = str(Path.cwd())

    entries = _load()
    existing_shas = {e.get("commit_sha") for e in entries if e.get("commit_sha")}

    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H|%an|%s", "--", "dci-hooks_*/"],
            capture_output=True, text=True, cwd=repo_root, timeout=30,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"git log failed: {result.stderr}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    human_commits = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, author, subject = parts

        if sha in existing_shas:
            continue
        if AGENT_COMMIT_PATTERN.search(subject):
            continue
        if author == AGENT_COMMITTER:
            continue

        human_commits.append((sha, author, subject))

    ingested = 0
    for sha, author, subject in human_commits:
        try:
            diff_result = subprocess.run(
                ["git", "diff", f"{sha}~1..{sha}", "--name-only", "--", "dci-hooks_*/"],
                capture_output=True, text=True, cwd=repo_root, timeout=10,
            )
            files_changed = [f for f in diff_result.stdout.strip().splitlines() if f]
        except Exception:
            files_changed = []

        try:
            diff_stat = subprocess.run(
                ["git", "diff", f"{sha}~1..{sha}", "--stat", "--", "dci-hooks_*/"],
                capture_output=True, text=True, cwd=repo_root, timeout=10,
            )
            diff_summary = diff_stat.stdout.strip()[-200:] if diff_stat.stdout else ""
        except Exception:
            diff_summary = ""

        record_fix(
            error_pattern=f"[human fix] {subject}",
            diagnosis=f"Human commit by {author}: {subject}",
            fix_applied=diff_summary or subject,
            files_changed=files_changed,
            success=True,
            source="human",
            commit_sha=sha,
        )
        ingested += 1

    return {
        "success": True,
        "commits_scanned": len(result.stdout.strip().splitlines()),
        "human_commits_found": len(human_commits),
        "ingested": ingested,
        "skipped_existing": len(human_commits) - ingested,
    }
