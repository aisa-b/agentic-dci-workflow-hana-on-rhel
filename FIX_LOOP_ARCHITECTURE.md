# Fix Loop Architecture — Agent ↔ Code Interaction

> **Status:** IMPLEMENTED. Gate functions in `agents/local/fix_loop.py`,
> PreToolUse hook in `.claude/hooks/enforce-step-order.sh`, configured
> in `.claude/settings.json`. Design principle: LLM decides intent,
> code enforces invariants.

## Overview

```mermaid
graph TB
    subgraph "Operator"
        USER[Operator Mac]
    end
    
    subgraph "Claude Code Session"
        SKILL["/dci-run Skill<br/>(agent reasoning)"]
        GATES["skill_api.py FixLoop<br/>(code gates)"]
        SUB_DIAG["dci-diagnostician<br/>(subagent)"]
        SUB_HANA["hana-expert<br/>(subagent)"]
        SUB_OSDEP["os-deploy-expert<br/>(subagent)"]
        SUB_REVIEW["ansible-reviewer<br/>(subagent)"]
        STATE["fix_loop_state.json<br/>(persisted state)"]
        KB["knowledge_base.json"]
        JOURNAL["run_journal.jsonl"]
    end
    
    subgraph "Remote (via Pub/Sub)"
        MCP["MCP Tools"]
        RELAY["Relay Daemon"]
        JUMPBOX["Jumpbox"]
        TARGET["Target Server"]
    end
    
    USER -->|"/dci-run host RHEL-9.8"| SKILL
    SKILL <-->|"submit_*() / result"| GATES
    GATES <-->|"read/write"| STATE
    SKILL -->|"delegate"| SUB_DIAG
    SKILL -->|"delegate"| SUB_HANA
    SKILL -->|"delegate"| SUB_OSDEP
    SKILL -->|"delegate"| SUB_REVIEW
    SUB_DIAG -->|"dci_ssh_execute"| MCP
    SUB_HANA -->|"dci_ssh_execute"| MCP
    SUB_OSDEP -->|"dci_jumpbox_execute"| MCP
    SKILL -->|"dci_workflow_run"| MCP
    MCP --> RELAY --> JUMPBOX --> TARGET
    GATES -->|"record"| KB
    GATES -->|"record"| JOURNAL

    style USER fill:#1a1a2e,stroke:#e94560,color:#fff,stroke-width:2px
    style SKILL fill:#0f3460,stroke:#e94560,color:#fff,stroke-width:2px
    style GATES fill:#533483,stroke:#e94560,color:#fff,stroke-width:2px
    style SUB_DIAG fill:#16213e,stroke:#0f3460,color:#fff
    style SUB_HANA fill:#16213e,stroke:#0f3460,color:#fff
    style SUB_OSDEP fill:#16213e,stroke:#0f3460,color:#fff
    style SUB_REVIEW fill:#16213e,stroke:#0f3460,color:#fff
    style STATE fill:#1a1a2e,stroke:#533483,color:#fff
    style KB fill:#1a1a2e,stroke:#533483,color:#fff
    style JOURNAL fill:#1a1a2e,stroke:#533483,color:#fff
    style MCP fill:#e94560,stroke:#fff,color:#fff,stroke-width:2px
    style RELAY fill:#c70039,stroke:#fff,color:#fff
    style JUMPBOX fill:#900c3f,stroke:#fff,color:#fff
    style TARGET fill:#581845,stroke:#fff,color:#fff
```

## State Machine

```mermaid
stateDiagram-v2
    [*] --> TRIAGE: Workflow fails
    
    TRIAGE --> TRIAGE: submit_triage() rejected\n(missing fields)
    TRIAGE --> PLAN: submit_triage() accepted\n(all fields filled)
    
    PLAN --> PLAN: submit_plan() rejected\n(missing fields)
    PLAN --> FIX: submit_plan() accepted
    
    FIX --> FIX: submit_fix() rejected\n(no review / review REJECT)
    FIX --> VERIFY: submit_fix() accepted\n(review APPROVE)
    
    VERIFY --> DONE_SUCCESS: submit_result(success=true)
    VERIFY --> TRIAGE: submit_result()\n attempt < 5\n(new error fed back)
    VERIFY --> EXPLORATION: submit_result()\nattempt == 3
    VERIFY --> DONE_FAILURE: submit_result()\nattempt == 5
    
    EXPLORATION --> FIX: agent says fixable=true
    EXPLORATION --> DONE_FAILURE: agent says fixable=false
    
    DONE_SUCCESS --> [*]: Finalize (KB + PR + journal)
    DONE_FAILURE --> [*]: Finalize (revert + report + journal)

    classDef investigation fill:#0f3460,color:#fff,stroke:#e94560,stroke-width:2px
    classDef decision fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    classDef action fill:#c70039,color:#fff,stroke:#fff,stroke-width:2px
    classDef done fill:#1a1a2e,color:#fff,stroke:#e94560,stroke-width:3px
    
    class TRIAGE investigation
    class PLAN decision
    class FIX action
    class VERIFY decision
    class EXPLORATION investigation
    class DONE_SUCCESS,DONE_FAILURE done
```

## Detailed Sequence — Attempt 1 (Full Happy Path)

```mermaid
sequenceDiagram
    box rgb(30, 30, 50) Operator Side
    participant OP as Operator
    end
    box rgb(15, 52, 96) Agent Reasoning
    participant SKILL as Agent (Skill)
    end
    box rgb(83, 52, 131) Code Gates
    participant GATES as Code (FixLoop)
    end
    box rgb(22, 33, 62) Subagents
    participant DIAG as dci-diagnostician
    participant HANA as hana-expert
    participant OSDEP as os-deploy-expert
    participant REV as ansible-reviewer
    end
    box rgb(199, 0, 57) Remote Infrastructure
    participant MCP as MCP Tools
    participant JB as Jumpbox
    end
    
    Note over OP,JB: WORKFLOW FAILS — entering fix loop
    
    OP->>SKILL: (no interaction — agent proceeds autonomously)
    
    rect rgb(30, 30, 50)
        Note over SKILL,GATES: STEP 2: Initialize
        SKILL->>SKILL: git checkout -b agent-fix/<timestamp>
        SKILL->>GATES: fix_loop(run_id, host, topic, error_output)
        GATES-->>SKILL: state=TRIAGE, attempt=0
    end
    
    rect rgb(15, 52, 96)
        Note over SKILL,JB: STEP 3: Triage (agent reasons freely)
        
        SKILL->>SKILL: Read error output, extract failing task
        SKILL->>SKILL: search_knowledge("error message")
        SKILL->>SKILL: grep -rn "error_value" dci-hooks/
        
        Note over SKILL: Agent finds file locally but<br/>doesn't know the correct value yet
        
        SKILL->>GATES: submit_triage(file="setup.yml", line=260,<br/>wrong="scsi-360...", correct="", ...)
        GATES-->>SKILL: REJECTED: missing correct_value<br/>Hint: read the file via cat on jumpbox
        
        Note over SKILL: Local analysis insufficient.<br/>Agent decides which subagent based on phase.
        
        alt Phase 1 failure
            SKILL->>OSDEP: Investigate OS deployment failure
            OSDEP->>MCP: dci_jumpbox_execute / dci_ssh_execute
            MCP-->>OSDEP: server state
            OSDEP-->>SKILL: findings + correct value
        else Phase 3-4 failure (HANA / benchmark)
            SKILL->>HANA: Investigate HANA failure
            HANA->>MCP: dci_ssh_execute (HANA-specific)
            MCP-->>HANA: HANA state
            HANA-->>SKILL: findings + correct value
        else Phase 2 or general failure
            SKILL->>DIAG: Investigate server state
            DIAG->>MCP: dci_ssh_execute / dci_ssh_diagnostics
            MCP-->>DIAG: diagnostics output
            DIAG-->>SKILL: findings + correct value
        else File content readable via jumpbox
            SKILL->>MCP: jumpbox_execute("cat setup.yml")
            MCP->>JB: cat setup.yml
            JB-->>MCP: file content
            MCP-->>SKILL: file content with correct value in comments
        end
        
        Note over SKILL: Agent now has all fields
        
        SKILL->>GATES: submit_triage(file="setup.yml", line=260,<br/>wrong="scsi-360...", correct="scsi-358...",<br/>evidence="from subagent/jumpbox cat",<br/>source="local_analysis | subagent name",<br/>failing_task="disk-init", phase=3)
        GATES-->>SKILL: ACCEPTED → state=PLAN
        GATES->>GATES: save state to fix_loop_state.json
    end
    
    rect rgb(83, 52, 131)
        Note over SKILL,GATES: STEP 4: Plan (agent reasons freely)
        
        SKILL->>SKILL: Formulate root cause, fix, confidence
        
        SKILL->>GATES: submit_plan(root_cause="other-server disk IDs active,<br/>target-server commented out",<br/>proposed_fix="swap comment lines",<br/>confidence="high",<br/>fallback="SSH to check actual disks",<br/>risk="low")
        GATES-->>SKILL: ACCEPTED → state=FIX<br/>file_to_edit=setup.yml, line=260
        GATES->>GATES: log_plan() to journal
    end
    
    rect rgb(88, 24, 69)
        Note over SKILL,REV: STEP 5: Fix + Review
        
        SKILL->>SKILL: Read file (cat from jumpbox if not local)
        SKILL->>SKILL: Write fixed version locally
        SKILL->>SKILL: git add + git commit
        
        Note over SKILL: Agent tries to submit without review
        
        SKILL->>GATES: submit_fix(sha="abc123",<br/>files=["setup.yml"],<br/>review_verdict="")
        GATES-->>SKILL: REJECTED: no review verdict.<br/>Delegate to ansible-reviewer first.
        
        SKILL->>REV: Review this change:<br/>setup.yml disk ID swap
        REV->>REV: Check YAML syntax, markers,<br/>variable refs, no-delete rule
        REV-->>SKILL: verdict=APPROVE
        
        SKILL->>GATES: submit_fix(sha="abc123",<br/>files=["setup.yml"],<br/>review_verdict="APPROVE")
        GATES-->>SKILL: ACCEPTED → state=VERIFY<br/>verbosity=0, attempt=1
        GATES->>GATES: log_fix_applied() to journal
    end
    
    rect rgb(144, 12, 63)
        Note over SKILL,JB: STEP 6: Push + Re-dispatch + Wait
        
        SKILL->>SKILL: git push -u origin HEAD
        SKILL->>SKILL: gh pr create (first attempt only)
        
        SKILL->>MCP: dci_workflow_run(verbosity=0, target=host)
        MCP->>JB: git pull + dci-rhel-agent-ctl
        
        Note over SKILL,JB: ~2 hours pass (agent monitors)
        
        SKILL->>MCP: dci_workflow_status(target=host)
        MCP-->>SKILL: status=completed, success=true
    end
    
    rect rgb(10, 135, 84)
        Note over SKILL,GATES: STEP 6c: Evaluate
        
        SKILL->>GATES: submit_result(success=true, phase_reached=5)
        GATES-->>SKILL: DONE_SUCCESS<br/>attempts=1<br/>next: "Finalize: record to KB,<br/>generate change report"
    end
    
    rect rgb(26, 26, 46)
        Note over SKILL,GATES: STEP 7: Finalize
        
        SKILL->>SKILL: record_fix() to KB
        SKILL->>SKILL: gh pr edit --body (change report)
        SKILL->>SKILL: end_run(success=True)
        SKILL-->>OP: Change report printed
    end
```

## Detailed Sequence — Attempt 1 Fails, Attempt 2 Succeeds

```mermaid
sequenceDiagram
    box rgb(15, 52, 96) Agent Reasoning
    participant SKILL as Agent (Skill)
    end
    box rgb(83, 52, 131) Code Gates
    participant GATES as Code (FixLoop)
    end
    box rgb(22, 33, 62) Subagents
    participant DIAG as dci-diagnostician
    participant REV as ansible-reviewer
    end
    box rgb(199, 0, 57) Remote
    participant MCP as MCP Tools
    end
    
    Note over SKILL,MCP: ATTEMPT 1: fix applied, workflow re-run...
    
    SKILL->>MCP: dci_workflow_status()
    MCP-->>SKILL: FAILURE at phase 3, task "disk-init"
    
    SKILL->>GATES: submit_result(success=false,<br/>phase_reached=3,<br/>failing_task="disk-init",<br/>progress_assessment="same",<br/>assessment_evidence="Same task, same error msg")
    GATES-->>SKILL: outcome=same, kept=false<br/>REVERT: git revert abc123<br/>state → TRIAGE<br/>attempt=1/5, verbosity_next=2
    GATES->>GATES: save state + log to journal
    
    Note over SKILL: Agent reverts the fix
    SKILL->>SKILL: git revert --no-edit abc123
    SKILL->>SKILL: git push
    
    rect rgb(255, 240, 220)
        Note over SKILL,DIAG: ATTEMPT 2: TRIAGE (deeper investigation)
        
        SKILL->>SKILL: Re-read error with verbosity=2 output
        SKILL->>SKILL: Local grep finds nothing new
        
        Note over SKILL: Agent decides local analysis<br/>is insufficient — delegates
        
        SKILL->>DIAG: Investigate disk-init failure on host.<br/>Target: host. Failing task: disk-init.<br/>Error: disk not found.<br/>Prior fix: swapped IDs, didn't help.
        
        DIAG->>MCP: dci_ssh_execute("lsblk")
        MCP-->>DIAG: disk layout
        DIAG->>MCP: dci_ssh_execute("ls /dev/disk/by-id/")
        MCP-->>DIAG: actual SCSI IDs on server
        DIAG-->>SKILL: Root cause: neither set of IDs<br/>matches actual hardware.<br/>Correct IDs: scsi-3NEW1, scsi-3NEW2
        
        SKILL->>GATES: submit_triage(file="setup.yml", line=260,<br/>wrong="scsi-358...",<br/>correct="scsi-3NEW1/3NEW2",<br/>evidence="dci-diagnostician SSH: lsblk shows...",<br/>source="dci-diagnostician",<br/>failing_task="disk-init", phase=3)
        GATES-->>SKILL: ACCEPTED → state=PLAN
    end
    
    Note over SKILL,GATES: PLAN → FIX → REVIEW → VERIFY (same flow as attempt 1)
    
    SKILL->>GATES: submit_result(success=true, phase_reached=5)
    GATES-->>SKILL: DONE_SUCCESS, attempts=2
```

## Detailed Sequence — Exploration Mode (After 3 Failures)

```mermaid
sequenceDiagram
    box rgb(15, 52, 96) Agent Reasoning
    participant SKILL as Agent (Skill)
    end
    box rgb(83, 52, 131) Code Gates
    participant GATES as Code (FixLoop)
    end
    box rgb(22, 33, 62) Subagents
    participant DIAG as dci-diagnostician
    participant HANA as hana-expert
    end
    box rgb(199, 0, 57) Remote
    participant MCP as MCP Tools
    end
    
    Note over SKILL,MCP: Attempts 1, 2, 3 all failed
    
    SKILL->>MCP: dci_workflow_status()
    MCP-->>SKILL: FAILURE (attempt 3 failed)
    
    SKILL->>GATES: submit_result(success=false,<br/>phase_reached=3,<br/>progress_assessment="same",<br/>assessment_evidence="...")
    GATES-->>SKILL: attempt=3/5, kept=false<br/>exploration_mode=true<br/>"Run at verbosity=4,<br/>delegate to all subagents<br/>before attempting fix 4."
    
    rect rgb(255, 220, 220)
        Note over SKILL,HANA: EXPLORATION MODE — no fixes, only investigation
        
        SKILL->>MCP: dci_workflow_run(verbosity=4)
        Note over SKILL,MCP: ~2 hours at max verbosity
        SKILL->>MCP: dci_workflow_status()
        MCP-->>SKILL: FAILURE with full debug output
        
        SKILL->>DIAG: Full server state assessment.<br/>3 prior fixes failed. Max verbosity output attached.
        DIAG->>MCP: dci_ssh_execute (multiple commands)
        DIAG-->>SKILL: OS-level findings
        
        SKILL->>HANA: Full HANA health report.<br/>3 prior fixes failed.
        HANA->>MCP: dci_ssh_execute (HANA-specific)
        HANA-->>SKILL: HANA-level findings
        
        Note over SKILL: Agent combines findings,<br/>decides: fixable or give up?
    end
    
    alt Agent determines fixable
        SKILL->>GATES: submit_triage(file=..., line=...,<br/>correct_value=...,<br/>evidence="combined subagent findings",<br/>source="combined")
        GATES-->>SKILL: ACCEPTED → continue to PLAN
        Note over SKILL,GATES: Attempt 4 proceeds normally
    else Agent determines unfixable
        SKILL->>GATES: submit_result(success=false,<br/>progress_assessment="unfixable",<br/>assessment_evidence="Hardware issue / upstream bug / ...")
        Note over SKILL: Skip to DONE_FAILURE
        GATES-->>SKILL: DONE_FAILURE<br/>Finalize: revert + report
    end
```

## Detailed Sequence — Finalize on Failure

```mermaid
sequenceDiagram
    box rgb(15, 52, 96) Agent Reasoning
    participant SKILL as Agent (Skill)
    end
    box rgb(83, 52, 131) Code Gates
    participant GATES as Code (FixLoop)
    end
    
    Note over SKILL,GATES: All 5 attempts exhausted or agent declared unfixable
    
    GATES-->>SKILL: DONE_FAILURE<br/>fixes_to_revert: [sha2, sha4]<br/>fixes_kept: [sha1, sha3]<br/>(sha1 and sha3 advanced progress)
    
    rect rgb(255, 230, 230)
        Note over SKILL: STEP 7: Finalize (failure path)
        
        loop For each fix to revert
            SKILL->>SKILL: git revert --no-edit <sha>
        end
        SKILL->>SKILL: git push origin HEAD
        
        SKILL->>SKILL: gh pr create --title "FAILED" --body<br/>"## Failure Report<br/>### Attempt 1: ...<br/>### Attempt 2: ...<br/>### Fixes Kept: [sha1, sha3]<br/>### Root Cause Analysis: ...<br/>### Recommendations: ..."
        
        loop For each attempt
            SKILL->>SKILL: record_fix(success=false, ...)
        end
        
        SKILL->>SKILL: end_run(success=False,<br/>fixes_kept=[sha1,sha3],<br/>fixes_reverted=[sha2,sha4])
    end
```

## Gate Validation Rules

```mermaid
graph LR
    subgraph "submit_triage()"
        T0[action_type] --> TV{All present?}
        T1[file_path] --> TV
        T2[line] --> TV
        T4[correct_value] --> TV
        T5[evidence] --> TV
        T6[failing_task] --> TV
        T7[phase] --> TV
        TV -->|NO| TR[REJECTED + hints]
        TV -->|YES| TA[ACCEPTED → PLAN]
    end
    
    subgraph "submit_plan()"
        P1[root_cause] --> PV{All present?}
        P2[proposed_fix] --> PV
        P3[confidence] --> PV
        PV -->|NO| PR[REJECTED]
        PV -->|YES| PA[ACCEPTED → FIX]
    end
    
    subgraph "submit_fix()"
        F1[commit_sha] --> FV{SHA + review?}
        F2[review_verdict] --> FV
        FV -->|No SHA| FR1[REJECTED: commit first]
        FV -->|No review| FR2[REJECTED: review first]
        FV -->|REJECT verdict| FR3[REJECTED: revise fix]
        FV -->|SHA + APPROVE| FA[ACCEPTED → VERIFY]
    end
    
    subgraph "submit_result()"
        R1[success] --> RV{Success?}
        RV -->|YES| RS[DONE_SUCCESS]
        RV -->|NO| RC{Assessment<br/>provided?}
        R2[progress_assessment] --> RC
        R3[assessment_evidence] --> RC
        RC -->|NO| RR[REJECTED: assess first]
        RC -->|YES| RD{Attempt 5?}
        RD -->|YES| RF[DONE_FAILURE]
        RD -->|NO| RT[→ TRIAGE<br/>new error fed back]
    end

    style T0 fill:#0f3460,color:#fff
    style T1 fill:#0f3460,color:#fff
    style T2 fill:#0f3460,color:#fff
    style T4 fill:#0f3460,color:#fff
    style T5 fill:#0f3460,color:#fff
    style T6 fill:#0f3460,color:#fff
    style T7 fill:#0f3460,color:#fff
    style TV fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    style TR fill:#c70039,color:#fff,stroke-width:2px
    style TA fill:#0a8754,color:#fff,stroke-width:2px
    style P1 fill:#0f3460,color:#fff
    style P2 fill:#0f3460,color:#fff
    style P3 fill:#0f3460,color:#fff
    style PV fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    style PR fill:#c70039,color:#fff,stroke-width:2px
    style PA fill:#0a8754,color:#fff,stroke-width:2px
    style F1 fill:#0f3460,color:#fff
    style F2 fill:#0f3460,color:#fff
    style FV fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    style FR1 fill:#c70039,color:#fff
    style FR2 fill:#c70039,color:#fff
    style FR3 fill:#c70039,color:#fff
    style FA fill:#0a8754,color:#fff,stroke-width:2px
    style R1 fill:#0f3460,color:#fff
    style R2 fill:#0f3460,color:#fff
    style R3 fill:#0f3460,color:#fff
    style RV fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    style RC fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    style RD fill:#533483,color:#fff,stroke:#e94560,stroke-width:2px
    style RS fill:#0a8754,color:#fff,stroke-width:2px
    style RR fill:#c70039,color:#fff
    style RF fill:#c70039,color:#fff,stroke-width:2px
    style RT fill:#e94560,color:#fff,stroke-width:2px
```

## Data Flow

```mermaid
graph TD
    subgraph "Persisted State"
        FS[fix_loop_state.json<br/>- current state<br/>- attempt count<br/>- triage/plan/fix data<br/>- fixes history<br/>- previous error]
        KB[knowledge_base.json<br/>- error patterns<br/>- successful fixes<br/>- server states]
        JR[run_journal.jsonl<br/>- every event<br/>- causal chains<br/>- timing data]
    end
    
    subgraph "Per-Attempt Data"
        ERR[Error Output] -->|"attempt N"| TRIAGE_DATA
        TRIAGE_DATA[Triage Result<br/>file, line, values] --> PLAN_DATA
        PLAN_DATA[Plan<br/>root cause, fix] --> FIX_DATA
        FIX_DATA[Fix<br/>SHA, files] --> RESULT_DATA
        RESULT_DATA[Result<br/>assessment, evidence] --> NEXT[Next Error<br/>or DONE]
    end
    
    TRIAGE_DATA -->|logged| JR
    PLAN_DATA -->|logged| JR
    FIX_DATA -->|logged| JR
    RESULT_DATA -->|logged| JR
    RESULT_DATA -->|on completion| KB
    
    FS -->|"survives context<br/>compression"| FS
```

## Tool Access Per Actor

| Actor | Can Read | Can Write | Can Ask User | MCP Tools |
|-------|----------|-----------|-------------|-----------|
| Agent (main skill) | Read, Grep, Glob, Bash | Edit, Write, Bash(git) | YES (but gates discourage) | All MCP tools |
| dci-diagnostician | Read, Grep, Glob, Bash | NO | NO (structurally blocked) | dci_ssh_execute, dci_ssh_diagnostics, dci_jumpbox_execute |
| hana-expert | Read, Grep, Glob, Bash | NO | NO (structurally blocked) | dci_ssh_execute, dci_ssh_diagnostics |
| os-deploy-expert | Read, Grep, Glob, Bash, WebFetch | NO | NO (structurally blocked) | dci_ssh_execute, dci_ssh_diagnostics, dci_jumpbox_execute |
| ansible-reviewer | Read, Grep, Glob, Bash | NO | NO (structurally blocked) | None |
| Code (FixLoop) | fix_loop_state.json | fix_loop_state.json | NO (it's code) | None (agent makes MCP calls) |
