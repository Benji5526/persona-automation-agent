---
name: pa-testing
description: Use when writing, running or reviewing tests for persona-automation-agent: tests/db, tests/bridge, failure and recovery tests, security tests, E2E, and the completion checklist.
---

# Testing

## Levels

| Level | Where | What |
|---|---|---|
| Unit | `tests/bridge/test_units.py` | validation, builders, redaction |
| Integration | `tests/db`, `tests/bridge` | DB functions and RLS on a real local Postgres; the bridge against it with a fake ComfyUI |
| E2E | TECH_DESIGN 27.4–27.8 | real Lovable → … → Asset (and later → Post → Analytics → Decision) |
| Failure & recovery | TECH_DESIGN 27.5, 27.6 | listed below |
| Security | TECH_DESIGN 27.7 | secrets, isolation, auth |

Run: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests -q` (Windows Git Bash; cp949 breaks otherwise).

## Always add

- A cross-account test for every new table or RPC (other user gets 0 rows / `PT404`; direct writes denied).
- A concurrent-claim test for any new claim function (two connections at once).
- An idempotency test (same key twice → one result) and a lost-response test for any external call.
- A malicious-LLM-output test for every new LLM schema (extra keys, forged refs, numbers in sentences, wrong persona).

## Failure tests that must exist

ComfyUI down (before and during generation) · bridge timeout · GPU OOM · missing model / LoRA · SNS token expired · duplicate publish and lost publish response · n8n restart / crash · DB error mid-transaction · AI malformed JSON · AI prompt injection · emergency stop · stale heartbeat recovery · registered-asset-before-crash (no duplicate asset).

## Done means

Tick `docs/MVP_CHECKLIST.md` section 1 for the feature. Tests passing is necessary, not sufficient. Report failing tests with their output; never hide them. Do not use `skip` / `only` / stub tests as evidence.

## Personal Edition

- Use GPU-neutral ids in tests (`python:local-1`, `python:cloud-1`); never assert a specific GPU model.
- Required tests (`tests/db/test_execution_targets.py`, `tests/db/test_worker_auth.py`, `tests/bridge`):
  - **Target**: default is `local`; an explicit override wins; an invalid target is rejected; the target is stored on the content job and inherited by the automation job (and cannot diverge); a direct client INSERT cannot choose a target; **changing the default does not move existing jobs**.
  - **Movement**: pending local→cloud and cloud→local; a running job and a finished job are never moved; the returned count is right; an audit event is written; non-admin and invalid targets are rejected.
  - **Claiming**: a local worker claims local and not cloud; a cloud worker claims cloud and not local; an unregistered/unknown worker cannot claim a cloud job; worker identity comes from JWT claims, not from the RPC argument; non-generation job types are unaffected; a refused claim leaves `status` and `attempts` untouched.
  - **Capability**: enough VRAM passes; insufficient VRAM, a missing model, a missing LoRA and a missing required workflow file all fail **non-retryably**, before ComfyUI is called. Assert the distinction from a runtime `OUT_OF_MEMORY` (retryable).
  - **Worker auth**: a valid JWT works; expired credential, expired token, revoked credential, wrong target, wrong worker id and missing claims all fail; an unauthorized RPC and any direct table read fail with `42501`; a worker cannot read SNS tokens, another persona, unrelated assets or a job it did not claim; a worker may revoke only its own credential; revocation stops every further RPC; the worker then stops claiming and exits cleanly without retrying.
- The pull loop still needs: claims when idle, does not claim while ComfyUI is down (attempts stay 0), does not claim while busy.
- **Security gate tests** (keep them; they encode vulnerabilities that were found and fixed):
  - `tests/db/test_worker_claim_security.py` — a broken credential errors (never claims a *local* job), non-generation jobs cannot be claimed by a worker, another worker cannot heartbeat/complete/fail/log/register on a claimed job even with a leaked `locked_at`, audit events and status reports cannot be forged, `authenticated`/`anon` cannot call worker RPCs.
  - `tests/db/test_worker_storage_isolation.py` — read only what the claimed job references, write only the claimed job's persona path, wrong persona / unclaimed / another worker's job / revoked / expired / traversal / overwrite / no UPDATE-DELETE.
  - `tests/db/test_worker_lifecycle.py` — concurrent claim has one winner, `FOR UPDATE SKIP LOCKED`, crash and revoked-worker recovery, retry keeps the target, capability failures stay final.
  - `tests/db/test_permission_surface.py` — the exact allowlist/denylist read from the live DB.
  - `tests/test_architecture_invariants.py`, `tests/test_protected_files.py` (hash manifest of the protected files and TECH_DESIGN 9-55), `tests/db/test_migrations_upgrade.py` (fresh install + upgrading a DB that already has jobs), `tests/db/test_verify_production_sql.py` (the hosted check queries actually run).
- **Prove a security test can fail.** After writing one, break the defence on purpose (mutation) and confirm the test goes red, then restore the file and compare hashes. `scripts/smoke_worker_jwt.py` logic is tested against a mock platform — that is **not** hosted verification, and a hosted check that was not run is reported `not-runnable`, never `passed`.
