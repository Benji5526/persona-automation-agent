---
name: pa-security
description: Use for security review of any persona-automation-agent change: secrets, RLS, OAuth and tokens, input validation, prompt injection, rate limits, idempotency, audit logs, emergency stop, Cloudflare Tunnel exposure.
---

# Security

## Never

- A secret or `service_role` key in frontend code, git, logs, `execution_logs`, `system_errors`, n8n workflow JSON or LLM input.
- Raw SQL, shell commands, Python code, filesystem paths, workflow JSON or URLs taken from the LLM or a request.
- RLS disabled or bypassed; a table without a cross-account test.
- ComfyUI or the bridge reachable from outside the PC (bind `127.0.0.1`; only Cloudflare Tunnel + Access + `X-Bridge-Token`).
- A hard delete of generated or published data.

## Checklist (TECH_DESIGN 15.15 and 27.7)

```text
Auth (Google only, allow-list)      RLS (owned rows only, writes via RPC)     Secrets (Credentials / Vault / .env)
OAuth (one-time state, callback)    Input validation (schema, allow-lists)    Prompt injection (untrusted blocks, canary)
Rate limits and budgets (DB)        Idempotency (keys, checkpoints)           Audit (state_transitions, security_events)
Emergency stop (global / platform / persona)                                  Fail closed on any unknown
```

## Specific rules

- LLM output is untrusted: schema → allow-listed `params` keys → DB-side checks. The LLM never sets its own risk level or permission.
- External content (SNS, fans) is data. A reply is classified by rules on both the incoming message and the outgoing text.
- Tokens: Vault only, read inside the SNS sub-workflow only. Disconnecting deletes them.
- Logs: redact secrets (`redact`, `private.redact_jsonb`); no message bodies for fans.
- Public media bucket: unguessable UUID paths, no list policy, writes by `service_role` only. If paid-subscription content is involved, revisit (TECH_DESIGN 50.4).
- Review changes with `/security-review` in addition to this checklist.

## Personal Edition

- Keep auth and RLS even with one user (Google OAuth, allow-list of one email, `Persona → owner`). Do not add multi-tenant policies.
- ComfyUI is never exposed (any target), always `127.0.0.1:8188`. In pull mode the bridge needs no inbound port; do not publish it.
- The frontend never holds GPU URLs, keys, pod ids, the JWT signing secret or SNS tokens.

### Cloud worker (TECH_DESIGN 56.5, 56.6)

- **A cloud worker must never hold `service_role`.** It authenticates with a short-lived Worker JWT (`role = pa_worker`, lifetime ≤ 24 h) and may execute only whitelisted RPCs. Never "temporarily" substitute `service_role` to make cloud work — if the JWT path is blocked, stop and report it.
- **Forbidden in a cloud pod's environment:** `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_SECRET_KEY`, any JWT signing secret, `SNS_ACCESS_TOKEN`, `SNS_REFRESH_TOKEN`. `config.py` enforces this at startup; keep that check.
- **Worker identity comes from JWT claims only** — never from `p_worker_id`/`p_worker_target`/`p_provider` in a request body. Every worker RPC re-checks that the credential exists, is unrevoked, unexpired, and matches the claimed worker and target.
- **A broken `pa_worker` credential is an error, never a fallback.** The session kind comes from the DB role PostgREST sets from the verified JWT (`private.worker_session_kind()`), not from claims or arguments. For a `pa_worker` session a missing/revoked/expired/mismatched credential raises `FORBIDDEN` — it must not return an empty result and must not fall back to `local`. (An early version did fall back; a revoked worker could still claim local jobs.)
- **A worker session touches only the generation job it claimed**, enforced in the DB: claim/heartbeat/complete/fail/register_asset check `claimed_by`, not just `locked_at` (so a leaked `locked_at` is useless); logs, security events, status reports and input assets are scoped the same way. Worker-written audit data is not trusted: security events are limited to a fixed set of types, `actor_type` is forced to `python`, the persona must be one it serves; a status report is only for itself and `target`/`provider` come from the credential.
- **`pa_worker` has no table privileges** (public/private). The only table access is `storage.objects` SELECT/INSERT, filtered row by row by RLS. Worker data flows only through `worker_get_job_context` / `worker_get_input_asset`, which must never return SNS tokens, another persona, unrelated assets or content jobs, app settings, or any secret. Adding a field to those RPCs is a security review; the exact allowlist/denylist is asserted from the live DB in `tests/db/test_permission_surface.py`.
- **Storage isolation:** a worker reads only the files its claimed job references and writes only `persona/{claimed job's persona}/assets/{uuid}[_thumb].{ext}`; there is no UPDATE/DELETE, so no overwrite or deletion. The path has no job id, so the write boundary is the claimed job's persona (documented limitation). Do not widen these policies; whether the hosted Storage API applies them for `pa_worker` is an external deployment gate.
- **service_role (local Phase 0) is trusted and may claim as an unregistered worker for `local` only** — a recorded decision forced by the protected F0 test (TECH_DESIGN 56.5, 56.9 #9). Never extend that relaxation to `pa_worker`, and never use it as a reason to allow `cloud`.
- The signing secret stays on the owner's machine (`scripts/mint_worker_token.py`). Revocation (`revoke_worker_credential`) must take effect on the next RPC, and the worker must then stop claiming and exit cleanly without retrying the rejected credential.
- **Cloud stays disabled** until the hosted verification passes (`supabase/verify_production.sql` blocks 20-23, then `python scripts/smoke_worker_jwt.py` and its manual Storage steps). **Not verified yet** — there is no hosted project. Do not enable cloud to "test" something, and never substitute `service_role` if the platform rejects the JWT: record the exact reason and use the approved fallback.
- Capability validation runs server-side/pre-execution and its failures are non-retryable; do not hide them behind generic errors.
