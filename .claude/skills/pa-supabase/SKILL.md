---
name: pa-supabase
description: Use when adding or changing database tables, columns, RLS policies, RPC functions, Storage buckets or policies, Realtime, or migrations in persona-automation-agent. Migration-first and RLS-first.
---

# Supabase (DB / Auth / Storage / RLS / Realtime)

## Rules

- **Migration-first.** Every schema change is a new file in `supabase/migrations/`. Applied migrations are never edited; later changes use `create or replace` / new migrations (TECH_DESIGN 21.3, 24.7).
- **RLS-first.** New tables enable RLS in the same migration. Reads: `persona_id in (select id from personas where user_id = auth.uid())`. Writes: RPC functions (`security definer`, `set search_path = ''`, check ownership with `auth.uid()`), not table policies. Worker RPCs: `revoke execute … from public, anon, authenticated; grant … to service_role` (TECH_DESIGN 15.4).
- `service_role` is backend only (n8n, bridge). The frontend uses the publishable key.
- No destructive migrations. Foreign keys are `restrict`; archive instead of delete.
- Read the current schema (migrations, types, indexes, policies) before editing. Never invent columns.
- Status values are the DB values (TECH_DESIGN 21.6).
- State changes go through RPC and the transition triggers (11.12); log with `state_transitions`.

## Standard flow

```text
Migration → RLS + grants → Index → tests/db → generated types (supabase gen types) → Hook (query / RPC) → UI
```

⚙️ There is no Repository layer: hooks call Supabase directly; shared query builders live in `src/lib/…/queries.ts` (TECH_DESIGN 18.7).

## Checks

- `tests/db` covers the new function and a cross-account denial.
- `supabase/verify_production.sql` still passes (add checks for new indexes/policies).
- Storage: `media` is public (generated assets, no list policy), `persona-private` is private (reference images, signed URLs); paths never come from the frontend (TECH_DESIGN 21.14).
- Realtime: add only the tables the UI subscribes to (TECH_DESIGN 21.15).

## Personal Edition

- Additive migrations only (next: 0012). `0001`–`0009` are applied and immutable; `0010_execution_targets.sql` and `0011_worker_auth.sql` are the execution architecture (TECH_DESIGN 56.4). Do **not** create SaaS tables (`organizations`, `memberships`, `subscriptions`, `persona_members`, per-customer limits) or rename `users.role` values.
- Settings: `app_settings.app_mode = 'personal'`, `app_settings.default_execution_target` (`'local'` default). There is **no `active_worker`** — do not reintroduce an equivalent single-worker setting.
- `execution_target` lives on `content_jobs` (fixed at creation, column default `private.default_execution_target()`) and on `automation_jobs` (inherited from the content job by trigger — the two can never diverge). It is deliberately **absent from the `authenticated` column-level INSERT/UPDATE grants**, so a direct client INSERT cannot choose a target; only the RPCs can. Keep it that way.
- The claim functions enforce the target inside the DB: a generation job is claimable only when its target equals the worker's **server-resolved** target (Worker JWT claims → `worker_status` report → `local`). Never trust `p_worker_target`/`p_provider`/`p_worker_id` from the request body.
- Moving jobs is an explicit admin action (`move_pending_jobs_to_target`, pending generation jobs only, returns the count, writes a security event). Changing the default target must never move jobs. Retrying on another target is explicit (`retry_automation_job(p_job_id, p_execution_target)`); no automatic failover.
- Worker auth (`0011`): `worker_credentials` is the register of issued `jti`s — **never store the signing secret or the token**. `pa_worker` is `NOLOGIN` with no table privileges; give it EXECUTE on worker RPCs only, and add new worker-facing data through `worker_get_job_context` rather than new table grants.
- **Every function a worker can call starts with `private.session_worker()`** (worker session → valid credential or `FORBIDDEN`; untrusted → `FORBIDDEN`; trusted → no-op) and scopes the job with `private.scoped_job()` / `lock_processing_job` (claimed by that worker, `job_type = 'generation'`). A new worker-callable function without this is a hole. Do not return an empty result for a broken credential and do not default to `local`.
- The set of functions `pa_worker` may EXECUTE is an exact allowlist (12 public RPCs + 2 private storage-policy predicates). Adding one means updating `tests/db/test_permission_surface.py` and `supabase/verify_production.sql` in the same change. `authenticated` must never get a worker RPC.
- Storage: worker access to `storage.objects` is two RLS policies (`pa_worker_read_inputs` SELECT, `pa_worker_write_results` INSERT) calling `private.worker_may_read_object` / `worker_may_write_object`. No UPDATE/DELETE privilege or policy. Test any change with `tests/db/test_worker_storage_isolation.py`.
- When you change a function's signature, drop the old one in the new migration and re-`grant execute` — leaving an overload lets callers skip the new logic. Never edit an applied migration file.
