"""
Skill API — the contract between Claude Code skills and the Python codebase.

Skills (.claude/skills/*/SKILL.md) contain `python3 -c "..."` blocks that
Claude executes as bash commands. Those blocks import from THIS module,
making the dependency explicit and discoverable by standard tooling.

If you rename or change a function signature here, update the corresponding
SKILL.md code blocks. The CI test `test_skill_api.py` validates that every
import in skill code blocks resolves correctly.

Usage in skills:
    python3 -c "from agents.skill_api import start_run; rid = start_run(...)"

This module re-exports functions from their source modules. The source
modules remain importable directly for use in Python code (MCP server,
tests, etc.).
"""

# -- Run Journal (event-sourced telemetry) --
from agents.local.run_journal import (  # noqa: F401
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
    search_diagnoses,
    find_pattern,
)

# -- Phase Expectations (world model + learned timing) --
from agents.local.phase_expectations import (  # noqa: F401
    check_phase_expectations,
    format_phase_report,
    detect_phase_number,
    is_phase_overdue,
    get_phase_timing,
)

# -- Fleet State (nr repetition tracking) --
from agents.local.fleet_state import (  # noqa: F401
    set_goal,
    record_completion,
    should_redispatch,
)

# -- Knowledge Base (fix history + success rates) --
from agents.local.knowledge_base import (  # noqa: F401
    search_knowledge,
)

# -- Fix Loop (handoff validation gates) --
from agents.local.fix_loop import (  # noqa: F401
    start_fix_loop,
    get_fix_loop,
    end_fix_loop,
    mark_subagent_used,
    submit_triage,
    submit_plan,
    submit_fix,
    submit_result,
    check_stuck,
)

# -- Hooks Sync (two-repo workflow) --
from tools.sync_hooks import sync_hooks  # noqa: F401
