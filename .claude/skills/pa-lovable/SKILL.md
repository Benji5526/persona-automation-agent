---
name: pa-lovable
description: Use when building or reviewing the Lovable/React frontend of persona-automation-agent: pages, hooks, Supabase queries, Realtime, approval UI, analytics views, and the Phase prompts in docs/lovable_master_prompt.md.
---

# Lovable frontend

## Architecture

```text
Page → Hook (TanStack Query) → Supabase (select / RPC)
```

No separate Repository layer (TECH_DESIGN 18.7). Shared query builders may live in `src/lib/`.

## Lovable does

UI, Google auth UI, CRUD for drafts, Supabase queries and RPC calls, Realtime, approvals, analytics views, monitoring views.

## Lovable never

Calls ComfyUI, the Python bridge, n8n, SNS APIs or an LLM; holds `service_role`, LLM keys or SNS tokens; writes `status` columns directly (state changes are RPCs); creates mock data or fake progress percentages.

## Rules

- Status values come from `src/lib/status.ts` and equal the DB values.
- Every page has loading, empty, error and permission states; RPC error codes are mapped to Korean messages (`src/lib/errors.ts`, TECH_DESIGN 17.12).
- Dangerous actions (cancel, archive, reject) ask for confirmation.
- Realtime subscriptions are cleaned up on unmount; the UI refetches on events.
- Progress is shown as **steps** (TECH_DESIGN 17.7), never a made-up percentage.
- AI decisions show action, evidence (numbers come from the stored Context), confidence (lower of AI confidence and evidence sample level), risk and approval status. No raw JSON editing.
- Generated media uses `public_url` / `thumbnail_url` (public bucket); signed URLs only for `persona-private`.
- Phase prompts live in `docs/lovable_master_prompt.md`; later phases (C, P, S, R, A, M) are written by Claude Code before they are sent (TECH_DESIGN 44.6, 44.7).

## Review checklist (Phase 6)

No call to anything but Supabase; searching the code for `sb_secret`, `service_role`, `8188`, `/v1/jobs`, `webhook` finds nothing.

## Personal Edition

- One real user (OWNER). Do not build organization, team, billing or tenant screens; `app_settings.app_mode = 'personal'`.
- **Execution Target, Worker and Provider are three different things** (TECH_DESIGN 56.2). Never merge them into one picker.
  - *Target* (`local` | `cloud`) = where a job runs. Settings has one admin radio bound to `app_settings.default_execution_target` (`update_app_setting`), and `/content-jobs/new` has a per-job override sent as `create_content_job(p_execution_target)`.
  - *Worker* = the instance that claimed it (`automation_jobs.claimed_by`); *Provider* = who supplies the GPU (`worker_status.provider`). Both are **reported** values, never settings.
- **A job's Target is fixed when it is created.** Changing the default must never move existing jobs: say so next to the radio, and offer a separate explicit action `move_pending_jobs_to_target` with the affected count, a confirmation and an audit event. It moves only *pending generation* jobs.
- Retrying on another target is explicit: `retry_automation_job(job_id, target)`. Never offer automatic local↔cloud failover.
- Worker cards show Target, Provider, Online/Offline, GPU, VRAM, installed models and installed LoRAs — all as reported. Worker credentials (admin) show worker_id, target, provider, expiry, revoked state and a revoke action; **the token value is never stored, so it can never be displayed**.
- The frontend never receives or sends GPU URLs, keys, pod ids, the JWT signing secret or SNS tokens, and never calls the bridge.
- Manual **Generate** = `create_content_job` RPC (then Realtime). Capability warnings from worker reports are an early hint only — execution-time validation is authoritative (56.7). When the target's worker is offline, show a waiting message, not a failure.
