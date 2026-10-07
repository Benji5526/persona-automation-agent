---
name: pa-architecture
description: Use when changing or reasoning about the overall persona-automation-agent architecture, layer responsibilities, job flow, or anything that touches more than one layer (Lovable, Supabase, n8n, Python bridge, ComfyUI, LLM). Start here before any other pa-* skill.
---

# Architecture

## The stack

```text
Lovable → Supabase → n8n → Python bridge (127.0.0.1:8000) → ComfyUI (127.0.0.1:8188) → GPU (local or cloud worker)
```

LLM path (decision layer only): `Context → LLM → structured JSON → validation (n8n early, DB final) → permission → approval → Job`.

Supabase is the **source of truth**. n8n holds no state of its own; a workflow that stops must be resumable from the database alone (TECH_DESIGN 20.1).

## Rules

- Do not cross layer responsibilities (CLAUDE.md section 3).
- The frontend never calls the execution services. The LLM never calls tools. Nothing executes without a Job.
- The bridge receives only `job_id`; it reads everything else from the claimed row.
- Decisions about retries, permissions, budgets and duplicates are made by **DB functions**, not by n8n or Python.
- Architecture change = update `docs/TECH_DESIGN.md` and `docs/MVP_SCOPE_LOCK.md` first (CLAUDE.md section 18).

## Where things are

| Need | Look at |
|---|---|
| Phase and milestone map | TECH_DESIGN 40.4, 44 |
| State machines | TECH_DESIGN 11 |
| Job chain and claims | TECH_DESIGN 11.4–11.6, 20.5 |
| What is in the MVP | `docs/MVP_SCOPE_LOCK.md` |
| Workflow numbering | TECH_DESIGN 14.3, 20.3, 40.7 |

## Before changing

1. Read the existing implementation. 2. Check the Scope Lock. 3. Pick the matching `pa-*` skills (SNS change → supabase + sns-publishing + testing + security; AI decision → supabase + ai-decision + testing + security; generation → python-execution + comfyui + supabase + testing + security). 4. Plan the migration and tests first.

## Personal Edition (CURRENT PRODUCT MODE = PERSONAL)

- Personal is the implementation target (one real user, OWNER = `users.role='admin'`). SaaS design is preserved in `docs/architecture/saas.md`; do **not** implement multi-user, organizations, billing, quotas, per-customer GPU or scaling unless explicitly asked. Do not delete SaaS docs or tables.
- The GPU is an **Execution Target** (local or cloud worker), chosen by `app_settings.active_worker`; workers pull jobs; the bridge interface and code are the same for both. Never hard-code a GPU model. See `docs/architecture/execution-targets.md`, TECH_DESIGN 56.
- Manual Generate and autonomous generation share one path: Content Job → n8n → active worker.
