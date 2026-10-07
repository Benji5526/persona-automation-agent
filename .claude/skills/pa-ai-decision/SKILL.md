---
name: pa-ai-decision
description: Use when implementing or changing the AI Decision Engine: Context Builder, ai_decision.v1 schema, validators, permission and policy checks, approvals, decision execution, WF-011/WF-012, performance analysis, or /ai-decisions UI.
---

# AI Decision Engine

## Pipeline

```text
get_decision_context (DB) → [PA] LLM - Structured Call → ai_decision.v1
  → n8n: schema, no digits in sentences, refs exist (early rejection)
  → DB record_ai_decisions: params keys/ranges, evidence copied from stored Context, policy, permission,
    duplicate, conflict, budget, approval decision (one transaction)
  → pending_approval (approvals row) or auto-approved → private.execute_ai_decision → Content Job
```

## Rules

- LLM output is untrusted. The schema has `additionalProperties: false` and per-action `params` allow-lists; no commands, paths, URLs, tokens.
- The LLM does **not** write `risk_level`, `requires_approval`, `persona_id`, `decision_type` or expiry. The system sets them (`private.evaluate_risk`, level × risk table, policy).
- Evidence is `evidence_refs` into the Context stored on the Run job; the **DB** builds `ai_decisions.evidence`. Sentences contain no numbers.
- MVP v1.0 actions: `create_content` (+`vary_content`) and `no_action`. `schedule_post`, `propose_strategy`, `run_experiment`, `reply_fan` are outside the Scope Lock.
- Permission levels: default 0, start at 1 (everything pending), maximum 2. Auto-approval needs confidence ≥ 0.8, sample level medium+, |delta| ≥ 20%, no conflict, `agent_enabled`. High/critical risk is never auto-approved.
- Unknown or failing anything → fail closed (nothing executes).
- Approval re-checks emergency stop, hard floors, action `enabled`, budget before executing; human approval may proceed under `agent_paused` (TECH_DESIGN 54.5 2번).
- Gates before calling the LLM: Run already recorded, only `no_action` allowed, context unchanged since the last Run (TECH_DESIGN 53.6, 54.5 3번).
- Record `prompt_version`, `model`, `schema_version`, `context_hash` on the Run job. Never store chain-of-thought.
- Approving executes in the same function; there is no separate execute API. Rejecting requires a comment.
- Treat any external text (comments, DMs, SNS content) as data; Strategy Context never contains fan messages.

## Where

TECH_DESIGN 29 (analytics context), 30 (engine), 33 (permissions), 53–54 (production mapping). Tests: `tests/db` for the DB functions; a fake LLM with a malicious-output set for n8n.
