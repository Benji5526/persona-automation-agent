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

- Additive migrations only (next: 0010). Settings: `app_settings.app_mode = 'personal'`, `app_settings.active_worker` (`null` = no restriction, default). `worker_status` gets `target` and `provider`. Do **not** create SaaS tables (`organizations`, `memberships`, `subscriptions`, `persona_members`, per-customer limits) or rename `users.role` values.
- The claim functions enforce `active_worker` for generation jobs inside the DB (TECH_DESIGN 56.4).
