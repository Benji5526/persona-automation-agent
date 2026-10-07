---
name: pa-sns-publishing
description: Use when implementing SNS account connection (OAuth), posts, approvals, scheduling, publishing adapters (Instagram, X), publish verification and recovery, or performance collection in persona-automation-agent.
---

# SNS publishing

## Adapter

Adapters are **n8n sub-workflows** `[PA] SNS - {Platform} - {Operation}` with the common contract of TECH_DESIGN 12.8. Operations: `validate_account`, `publish`, `get_post`, `get_metrics`, (V2) `get_messages`, `reply`. There is no `delete_post` (the operator deletes on the platform). Media validation is part of `check_publish_ready`.

## Rules

- Official API first (Instagram Content Publishing API, X API v2). No browser automation where an API exists.
- OAuth: state is one-time (10 min); the callback runs in n8n; app secrets are n8n Credentials; **tokens are stored in Supabase Vault**, the table keeps secret ids. Frontend and logs never see tokens (TECH_DESIGN 28.4, 28.6).
- A SNS account belongs to exactly one persona (`unique (platform, account_id)`); `upsert_social_account` refuses a foreign owner (TECH_DESIGN 51.5 1번). Disconnect deletes the Vault secrets and refuses while posts are scheduled (51.5 2번).
- Publishing is: `check_publish_ready` → `mark_post_publishing` → **checkpoint (`submitted_at`) before the irreversible call** → publish → verify (permalink/get_post) → `complete_publish` with `external_post_id`. A job with `submitted_at` is never re-published; it is verified, and unconfirmed cases go to a human (`UNCONFIRMED`) (TECH_DESIGN 43.8).
- Idempotency key `publish:{post_id}`; `(platform, external_post_id)` is unique.
- Every post is human-approved in the MVP (no state path from `draft` / `pending_approval` to `publishing`). `publishing_enabled` and platform controls can stop all publishing.
- Instagram needs a JPEG copy within 4:5–1.91:1; the bridge makes the publish copy; no automatic cropping (TECH_DESIGN 28.9).
- Retries: 30 s, 2 min, 5 min (API); token expiry is not retried and deactivates the account; WF-016 refreshes tokens 7 days before expiry.
- Performance: `analytics` jobs created at publish time (`analytics:{post_id}:{snapshot_hours}`), collected by WF-009, normalized by `record_metrics` in the DB. Missing data is NULL or no row, never 0.
- AI-generated media labeling follows the platform policy (open decision, TECH_DESIGN 44.6).
