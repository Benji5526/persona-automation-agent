---
name: pa-python-execution
description: Use when changing the Python bridge in app/ (FastAPI /v1, GPU worker, ComfyUI client, output validation, Storage upload, asset registration) or its tests in tests/bridge. Local execution layer.
---

# Python execution layer (bridge)

## Flow

```text
POST /v1/jobs { job_id } → claim_automation_job → build from DB rows → Workflow Registry → ComfyUI
  → output validation → Storage upload → register_asset → complete_automation_job
```

## Rules

- Bind `127.0.0.1`. Authenticate with `X-Bridge-Token` (rotation: two tokens); reachable from n8n only through Cloudflare Tunnel + Access.
- The request body is `job_id` only. Persona, content job, prompt parameters and LoRA come from the claimed rows. Reject any other key (`422`).
- GPU concurrency is 1. Heartbeat every 30 s; a lost lock (`locked_at` mismatch) means stop and discard the result.
- Only Registry workflows run. No arbitrary workflow JSON, shell, filesystem path or Python from the LLM or from the request.
- Validate outputs: filename allow-list (`pa/{job_id}_NNNNN_.ext`), size cap, decode, expected dimensions; failures are `OUTPUT_INVALID` / `OUTPUT_UNEXPECTED` / `OUTPUT_TOO_LARGE` (TECH_DESIGN 47.3).
- Before generating, check whether this job already has registered assets; if so complete it with those and do not generate again (TECH_DESIGN 49.5 1번).
- Errors use the codes in TECH_DESIGN 13.12; the bridge reports them, the DB decides the retry.
- No secrets in logs (`redact`). Never log prompts of fan messages.

## Where

`app/main.py`, `api.py`, `worker.py`, `database.py`, `comfyui/`, `security.py`, `storage.py`; config in `.env` (`.env.example` has names only).

## Tests

`tests/bridge` runs against a real local Postgres and a fake ComfyUI: `PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/bridge -q`. Add a test for every new error path (claim race, timeout, OOM downscale, lock loss, bad output).

## Personal Edition: local and cloud workers

- **One implementation for both.** The same code runs on the local PC and in a cloud GPU pod; only `.env` differs. Never fork into two worker implementations. `COMFY_URL` is always `127.0.0.1` (ComfyUI never exposed).
- **Worker Pull is the primary mechanism** (TECH_DESIGN 56.3): when idle and ComfyUI is up, claim the next generation job (`claim_next_automation_job`). Never claim when ComfyUI is down (attempts must not grow).
- **Python never decides local vs cloud.** Eligibility is the DB's job: the job's `execution_target` plus the authenticated worker identity. `WORKER_TARGET`/`WORKER_PROVIDER` are only *reported* in `report_worker_status`; do not re-implement the claim check in Python, and do not pick a target.
- **Auth abstraction** (`WORKER_AUTH`): `secret_key` (local Phase 0) or `worker_jwt` (the converged path). In `worker_jwt` mode the process holds only the Worker JWT and the public anon key — `config.py` **refuses to start** if a Supabase secret/service_role key, a JWT signing secret or an SNS token is present.
- **Auth failure is a controlled shutdown** (401 / revoked / expired; also `42501` = an RPC outside the `pa_worker` allowlist): stop claiming new jobs, safely stop the current job (cancel the ComfyUI prompt; the DB recovers the job through the missed heartbeat), clear the queue, request process exit (`app.state.request_exit` → uvicorn `should_exit`), and do it **once**. Never hammer Supabase with a credential that was just rejected — no failure report, no shutdown report. The redacted `WORKER_AUTH_FAILED` event is best-effort (a rejected credential usually cannot write it; the worker log is the record). `is_worker_auth_failure` tells credential failures apart from business errors such as `job is not claimed by this worker`, which must not stop the worker.
- **In `worker_jwt` mode call only allowlisted RPCs.** `sync_workflow_registry` is not one — it is skipped (the owner's local bridge syncs `comfy_workflows`), because calling it returns `42501`, which the worker would read as a rejected credential and shut itself down. The push endpoint (`POST /v1/jobs`) returns `503 PUSH_DISABLED`, and uploads never send `x-upsert` (no UPDATE privilege).
- **No direct table SELECTs for the worker path.** `pa_worker` has no table privileges: get everything from `worker_get_job_context(job_id)` (job, content job, persona execution config, persona assets, input assets, already-registered assets) and `worker_get_input_asset`. If you need a new field, extend that RPC — do not add a table grant.
- Report GPU name/VRAM from ComfyUI `/system_stats` (never configure or hard-code a GPU model) plus `target` and `provider`.
- **Capability preflight before ComfyUI** (56.7): `check_capability` (VRAM via `requirements.min_vram_gb`, `requirements.required_files`) then `check_models`. These fail **non-retryably** (`WORKFLOW_UNSUPPORTED_ON_TARGET`, `MODEL_NOT_FOUND`, `LORA_NOT_FOUND`). Keep them distinct from a runtime `OUT_OF_MEMORY`, which *is* retryable and triggers the downscale path.
- No inbound port is needed in pull mode; a cloud pod only makes outbound HTTPS calls to Supabase.
