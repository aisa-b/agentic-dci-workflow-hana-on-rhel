"""Validate that skill SKILL.md code blocks reference importable functions.

Parses every `python3 -c "from agents..."` block in skill files and
verifies the imports resolve. Catches signature drift between skills
and the Python codebase.
"""

import re
from pathlib import Path

import pytest


SKILLS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills"


def _extract_skill_imports(skill_path: Path) -> list[tuple[str, str]]:
    """Extract (module, name) pairs from python3 -c blocks in a SKILL.md."""
    text = skill_path.read_text()
    pattern = re.compile(r"from (agents\.\S+) import (.+)")
    results = []
    for match in pattern.finditer(text):
        module = match.group(1)
        names = [n.strip().rstrip(")") for n in match.group(2).split(",")]
        for name in names:
            if name and not name.startswith("#"):
                results.append((module, name))
    return results


def _get_all_skill_files() -> list[Path]:
    if not SKILLS_DIR.exists():
        return []
    return list(SKILLS_DIR.glob("*/SKILL.md"))


class TestSkillApiContract:
    def test_skill_api_imports_cleanly(self):
        from agents import skill_api  # noqa: F401

    def test_all_skill_imports_resolve(self):
        skill_files = _get_all_skill_files()
        assert len(skill_files) > 0, "No skill files found"

        failures = []
        for skill_path in skill_files:
            imports = _extract_skill_imports(skill_path)
            for module_path, func_name in imports:
                try:
                    mod = __import__(module_path, fromlist=[func_name])
                    assert hasattr(mod, func_name), f"{module_path}.{func_name} not found"
                except (ImportError, AssertionError) as e:
                    failures.append(f"{skill_path.parent.name}: {module_path}.{func_name} — {e}")

        assert not failures, "Broken skill imports:\n" + "\n".join(failures)

    def test_skill_api_reexports_match_skills(self):
        """Every function imported in skills should be available via skill_api."""
        from agents import skill_api

        skill_files = _get_all_skill_files()
        missing = []
        for skill_path in skill_files:
            imports = _extract_skill_imports(skill_path)
            for _, func_name in imports:
                if not hasattr(skill_api, func_name):
                    missing.append(func_name)

        assert not missing, (
            f"Functions used in skills but missing from agents/skill_api.py: {sorted(set(missing))}"
        )

    @pytest.mark.parametrize("func_name", [
        "start_run", "end_run", "log_workflow_dispatched", "log_workflow_completed",
        "log_triage", "log_diagnosis", "log_plan", "log_fix_applied",
        "log_fix_reverted", "log_attempt_outcome", "search_diagnoses", "find_pattern",
        "check_phase_expectations", "format_phase_report", "detect_phase_number",
        "is_phase_overdue", "get_phase_timing",
        "set_goal", "record_completion", "should_redispatch",
        "search_knowledge",
    ])
    def test_skill_api_exports(self, func_name):
        from agents import skill_api
        assert hasattr(skill_api, func_name), f"skill_api missing: {func_name}"
        assert callable(getattr(skill_api, func_name)), f"skill_api.{func_name} not callable"
