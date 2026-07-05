#!/bin/bash
# PreToolUse hook: blocks actions belonging to fix loop steps the agent hasn't reached.
# Reads fix_loop_state.json and returns exit 2 (block) with feedback, or exit 0 (allow).
# Fires on subagent tool calls too — subagents also can't skip steps.

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // ""')

STATE_FILE="${CLAUDE_PROJECT_DIR}/agents/local/fix_loop_state.json"
if [ ! -f "$STATE_FILE" ]; then exit 0; fi

active=$(jq -r '.active // false' "$STATE_FILE" 2>/dev/null)
if [ "$active" != "true" ]; then exit 0; fi

triage=$(jq -r '.triage_accepted // false' "$STATE_FILE")
plan=$(jq -r '.plan_accepted // false' "$STATE_FILE")
review=$(jq -r '.review_approved // false' "$STATE_FILE")
committed=$(jq -r '.fix_committed // false' "$STATE_FILE")

case "$tool_name" in
    Edit|Write|MultiEdit)
        if [ "$plan" != "true" ]; then
            echo "Fix loop: cannot edit files before completing triage and plan. Call submit_triage() with your findings, then submit_plan()." >&2
            exit 2
        fi
        ;;
    Bash)
        cmd=$(echo "$input" | jq -r '.tool_input.command // ""')
        if echo "$cmd" | grep -qE "^git commit|^git add .* && git commit"; then
            if [ "$review" != "true" ]; then
                echo "Fix loop: cannot commit without ansible-reviewer approval. Delegate to ansible-reviewer first, then call submit_fix() with review_verdict=APPROVE." >&2
                exit 2
            fi
        fi
        if echo "$cmd" | grep -q "^git push"; then
            if [ "$committed" != "true" ]; then
                echo "Fix loop: cannot push without a committed fix. Call submit_fix() first." >&2
                exit 2
            fi
        fi
        ;;
    mcp__dci-relay__dci_workflow_run)
        if [ "$committed" != "true" ]; then
            echo "Fix loop: cannot dispatch workflow without a committed and pushed fix. Complete the fix step first." >&2
            exit 2
        fi
        ;;
esac

exit 0
