---
name: pa-fan-interaction
description: Use when working on fan conversations, messages, memory, reply drafting or sending, risk classification, or the fan-related workflows and screens. NOTE: fan interaction is OUTSIDE the MVP v1.0 scope lock; only touch it when explicitly asked.
---

# Fan interaction (post-MVP)

Outside `docs/MVP_SCOPE_LOCK.md`. Do not build or "prepare" it unless the user asks. Design: TECH_DESIGN 31, 55.

## Pipeline

```text
Webhook (signature check) → record_fan_message (idempotent, immediate) → reply_draft job (60 s debounce)
  → get_reply_context → rule classification → LLM (fan_reply.v1) → record_reply_proposal
  → auto (fan_reply_level ≥ 2, LOW only at first) or human approval → reply_send job → SNS → messages
  → memory job (fan_memory.v1) → apply_memory_changes
```

## Rules

- Fan input is untrusted data in its own block; never an instruction. The response is structured output only (`risk_categories`; the system decides the risk level).
- Rule-based classification runs on the fan message **and on the AI's reply**. High and critical risk always go to a human; critical alerts immediately.
- Never store sensitive data in Memory (contacts, address, accounts, health, sexual orientation, religion, politics, finances, third parties, anything about a suspected minor).
- Replies: idempotency `send:{ai_decision_id}`; checkpoint `submitted_at` before sending; unconfirmed sends are not re-sent automatically; the 24-hour DM window is enforced.
- Rate limits per conversation / persona, a fan-feature LLM budget, spam detection, repetition check, debounce.
- A human-edited reply or direct reply still gets the deterministic checks (secrets, `never_claim` / AI identity, length / window).
- Persona isolation: no cross-persona memory. Delete requests via `delete_fan_data`. No message bodies in logs.
- Emergency stop cancels not-yet-sent **auto-approved** replies; human-approved sends and direct replies continue; collection never stops.
