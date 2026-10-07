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
