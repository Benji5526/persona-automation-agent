---
name: pa-n8n
description: Use when creating or changing n8n workflows in n8n/ (dispatchers, generation, result handling, error handler, publishers, collectors, AI strategy runner) or the n8n deployment in deploy/n8n.
---

# n8n orchestration

n8n is the **orchestrator**, not the brain and not the state store.

## Rules

- Required in every job-handling workflow: **atomic claim** (`claim_*` RPC, 0 rows = stop quietly), **idempotency key**, timeouts per node, `execution_logs` steps (`CLAIM`, `LLM`, `COMPLETE`, …), error workflow (WF-006), recovery by safety-net polling.
- **Retry is decided by the DB**: report failures with `fail_automation_job(…, retryable, retry_after_seconds)`. Defaults: 30 s, 2 min, 5 min, max 3 attempts. Do not build retry loops in n8n (TECH_DESIGN 20.11).
- Trigger: DB Webhook for immediacy plus a 1-minute schedule as a safety net (20.4).
- Secrets only in n8n Credentials (`PA Supabase`, `PA Bridge`, `PA Anthropic`, …). No tokens in workflow JSON; `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`. Change addresses before import (n8n_guide 4-1).
- Call Supabase via PostgREST/RPC only (no SQL nodes). State changes only through Worker RPCs.
- The bridge call is `POST /v1/jobs {job_id}` and returns `202` immediately; do not wait for generation.
- LLM calls go through the sub-workflow `[PA] LLM - Structured Call`; validate its output (schema, no digits in sentences, refs exist) but the **DB makes the final decision** (TECH_DESIGN 30.8, 54.5).
- One execution handles one job (so the error handler can find it via the `CLAIM` log).
- Never put business intelligence in n8n that belongs in DB functions or the LLM Context Builder.

## Workflows (TECH_DESIGN 20.3)

WF-001 Content Job Dispatcher · 002 Prompt Generator · 003 Generation Dispatcher · 004 Result Handler · 005 Caption Generator · 006 Error Handler · LLM sub-workflow; V1: 007 SNS Publisher, 008 Scheduled Publisher, 009 Performance Collector, 010 Notification, 016 Token Refresh; V2: 011 Analyzer, 012 AI Strategy Runner, 013/014/017 fan workflows.

## Checks

Static validation of the JSON (valid JSON, node references, Code node syntax) and an end-to-end run with the fake LLM (`llm_mode = fake`) before enabling the real one.
