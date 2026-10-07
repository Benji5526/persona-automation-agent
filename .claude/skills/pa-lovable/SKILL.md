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
