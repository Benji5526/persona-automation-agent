# CLAUDE.md — persona-automation-agent

Rules for Claude Code in this repository. Read this first, then the documents it points to.

## 0. Sources of truth (read before changing anything)

| Document | Use it for |
|---|---|
| `docs/MVP_SCOPE_LOCK.md` | What is in / out of MVP v1.0. **Do not build anything outside it.** |
| `docs/TECH_DESIGN.md` | The design. Section numbers are cited as "TECH_DESIGN 20.5" etc. Start with section 40. |
| `docs/MVP_CHECKLIST.md` | The completion checklist. A feature is not done until its boxes are checked. |
| `docs/PRD.md` | Product scope and phases. |
| `.claude/skills/pa-*` | Task-specific rules (architecture, supabase, lovable, python-execution, n8n, comfyui, ai-decision, sns-publishing, fan-interaction, testing, security). |

If code and design disagree, **stop and report** (section 18). Do not guess which one wins.

Language: talk to the user in Korean. Documentation and code comments follow the existing files (Korean); identifiers are English.

## 1. Project identity

An autonomous virtual-influencer operating platform:

```text
Observe → Analyze → Decide → Create → Publish → Interact → Measure → Learn → Decide
```

The MVP prioritizes reliability, observability, safety and deterministic execution over feature count.

## 2. Architecture

```text
Lovable / React → Supabase → n8n → Python FastAPI bridge (127.0.0.1) → ComfyUI → RTX 5080
```

The LLM is the runtime intelligence layer (the provider is one sub-workflow; currently Anthropic Claude). Claude Code is the development assistant.

## 3. Responsibility boundaries

| Layer | Does | Must NOT |
|---|---|---|
| Lovable | UI, auth UI, CRUD, Supabase queries and RPC calls, Realtime, approval UI, analytics and monitoring views | call ComfyUI / Python / n8n / SNS directly, hold any secret, write a status column directly |
| Supabase | Auth, PostgreSQL, RLS, Storage, Realtime, state transitions, **final validation and permission checks** (DB functions) | call external services |
| n8n | orchestration, scheduling, dispatch, LLM calls, early schema rejection, notifications | decide retries (the DB does), hold business intelligence, hold state |
| Python bridge | execute one claimed job: ComfyUI, output validation, Storage upload, asset registration | make strategy decisions, answer fans, run arbitrary commands, accept anything but a `job_id` |
| ComfyUI | media generation only | be reachable from outside the PC |
| LLM | analysis, classification, structured proposals, captions | execute anything, set its own risk level or permission |

## 4. Absolute security rules

1. Never expose `service_role`/secret keys, LLM API keys or SNS tokens to the frontend, to git, or to n8n workflow JSON (use n8n Credentials; SNS tokens live in Supabase Vault).
2. Never disable RLS to fix an application problem. Never use `service_role` in browser code.
3. Never accept arbitrary filesystem paths, shell commands, Python code, SQL, or ComfyUI workflow JSON from users or the LLM.
4. SNS and fan content is untrusted input. Prompt injection must never override policy.
5. Emergency Stop overrides every autonomous action.

## 5. AI rules

- LLM output is untrusted structured JSON (`ai_decision.v1`, TECH_DESIGN 30.6). No free text, no tool calls.
- The **LLM supplies** `action`, `target_ref`, `params`, `priority`, `confidence`, `reasoning_summary`, `evidence_refs`, `expected_outcome`. The **system sets** `persona_id`, `decision_type`, `risk_level`, approval requirement and expiry. The LLM never writes `risk_level` or `requires_approval` (TECH_DESIGN 53.4).
- Sentences contain no numbers; evidence is a reference to the stored Context, and the DB copies the numbers (TECH_DESIGN 29.15, 54.5).
- Never store chain-of-thought. Store `reasoning_summary`, `evidence`, `confidence`.
- Execution chain, always:

```text
LLM → Structured Decision → Schema check (n8n, early) → DB function: policy, permission, budget, duplicate → Approval if required → Job → Execution
```

- The LLM cannot call SNS, n8n, Python, ComfyUI, the filesystem, the database or a shell.
- `no_action` is a normal decision.

## 6. Database rules

Never invent columns or tables. Before changing the database:

1. Read the migrations in `supabase/migrations/` (applied files are never edited).
2. Read the related TypeScript types and queries, existing indexes, existing RLS policies.
3. Add a **new** migration, with RLS and indexes, and a test in `tests/db`.
4. Never mutate a production schema by hand.

Status values are the database values (TECH_DESIGN 21.6, 11.x). Do not introduce alternative spellings.

## 7. RLS rules

Isolation chain: `auth.uid()` → `personas.user_id` → child tables by `persona_id`. Direct reads only for owned rows; **all writes to state go through RPC functions**. Frontend filtering is never a security measure. Add a cross-account test for every new table.

## 8. Job rules

All execution is a Job. The chain: Content Job → Automation Job (`prompt` / `generation` / `caption` / `publish` / `analytics` / `decision` …) → execution → Asset. A button click creates or changes state; a worker executes. Workers claim atomically (`claim_*` RPCs); only the claimant may write results (lock token `locked_at`).

## 9. Idempotency

Every external action has an idempotency key (TECH_DESIGN 14.17):

```text
generation:{content_job_id}:{run_number}   publish:{post_id}   analytics:{post_id}:{snapshot_hours}
send:{ai_decision_id}                      decision:{persona_id}:{platform}:daily:{date}
```

Before retrying an uncertain external action, check whether it already succeeded (checkpoint `submitted_at`, then verify-only; TECH_DESIGN 43.8). Never blindly retry.

## 10. Error handling

Never swallow errors. Record `error_type`, `error_code`, message, service, `retryable` in `system_errors` and `execution_logs`. The DB (`fail_automation_job`) decides retry or final failure: 30 s, 2 min, 5 min, at most 3 attempts. Non-retryable errors stop. Correlation is by foreign-key chain (Content Job → Automation Job → Asset → Post), not a separate ID (TECH_DESIGN 37.2).

## 11. Emergency Stop

Three scopes: global (`emergency_stop_all`), platform (`platform_controls`), persona (`agent_paused`). It blocks AI decisions, generation start, publishing, automatic fan replies and autonomous actions. It never deletes data. Collection, recovery and error recording continue (TECH_DESIGN 33.10).

## 12. Development rules

Before writing code: inspect the repository, the relevant files, the schema and the existing implementation; reuse existing patterns; do not duplicate functionality; do not rewrite working modules without a reason; prefer small reversible changes; no speculative abstraction.

Do not assume a feature belongs to the MVP unless `docs/MVP_SCOPE_LOCK.md` lists it.

## 13. Testing

Every feature needs unit, integration and (where it crosses a boundary) failure tests. Commands (Windows, Git Bash):

```bash
PYTHONUTF8=1 .venv/Scripts/python -m pytest tests -q
```

`tests/db` runs against a real local Postgres; `tests/bridge` runs the bridge against that DB and a fake ComfyUI. Minimum E2E: Google login → Persona → Content Job → generation → Asset → approval → Post → analytics → AI decision (TECH_DESIGN 27).

## 14. UI rules

Show real state; never fake metrics or success. Loading, empty, error and success states all exist. Dangerous actions need confirmation. AI decisions show action, evidence, confidence, risk and approval status. No raw JSON editing in the MVP. Copy is Korean.

## 15. Asset rules

Every generated asset keeps lineage: persona, content job, automation job, workflow, workflow version, model, LoRA, prompt, negative prompt, seed. Never overwrite an asset; regeneration creates a new one. Archive instead of deleting (there is no hard delete).

## 16. SNS rules

Use official APIs; no browser automation where an official API exists. Publishing is always: validate → checkpoint → publish → verify → persist the external ID. Adapters are n8n sub-workflows (`[PA] SNS - {Platform} - {Operation}`), tokens come from Vault inside them only.

## 17. Autonomy levels

Per-persona `agent_permission_level` 0 Observe, 1 Recommend (everything needs approval), 2 Create Content, 3 Generate + Schedule, 4 Generate + Publish, 5 Full (TECH_DESIGN 15.19, 30.9). **MVP v1.0: default 0, start at 1, maximum 2.** Fan replies use a separate `fan_reply_level`. No level bypasses policy, budget, rate limits or Emergency Stop.

## 18. When the architecture must change

1. Stop. 2. Explain the conflict. 3. List affected components. 4. Update `docs/TECH_DESIGN.md` / `docs/MVP_SCOPE_LOCK.md`. 5. Then implement. Never change the architecture silently.

## 19. Behavior

Act as a senior engineer: inspect before editing, explain significant decisions, preserve conventions, create migrations for schema changes, write tests, keep secrets out, respect the Scope Lock, prefer implementing over abstracting. Before saying "done", tick every box in `docs/MVP_CHECKLIST.md` section 1 for the feature, or write down why a box does not apply. A feature with only a UI is not done.

Commit and push only when asked.

## 20. Priority

```text
1 Authentication  2 Persona  3 Content Job  4 Generation  5 Asset  6 Approval
7 Publishing  8 Analytics  9 AI Decision  10 Autonomous Loop  11 Fan Interaction (outside MVP v1.0)
```

Stop after 10 if time is short.

## 21. Final rule

The system succeeds when a real Persona creates a real Content Job, generates a real Asset, publishes a real Post, receives real performance data, produces a validated AI Decision, and creates the next Content Job — and the whole cycle is observable, recoverable and safe.
