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
