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

- The same code runs on the local PC and in a cloud GPU pod. Only `.env` differs: `EXECUTION_TARGET` (`local`|`cloud`), `WORKER_ID` (e.g. `python:local-1`, `python:runpod-1`), `PULL_JOBS`. `COMFY_URL` is always `127.0.0.1` (ComfyUI never exposed).
- **Pull** (TECH_DESIGN 56.3): when idle and ComfyUI is up, claim the next generation job (`claim_next_automation_job`). Never claim when ComfyUI is down (attempts must not grow). The DB decides who may claim (`active_worker`); do not re-implement that check in Python.
- Report GPU name/VRAM from ComfyUI `/system_stats` (never configure or hard-code a GPU model) and report `target`/`provider`.
- Reject a workflow whose `requirements.min_vram_gb` exceeds the worker's VRAM with `WORKFLOW_UNSUPPORTED_ON_TARGET` (not retryable) before calling ComfyUI.
- Cloud pods use a dedicated Supabase secret key that is revoked after the pod ends. No inbound port is needed in pull mode.
