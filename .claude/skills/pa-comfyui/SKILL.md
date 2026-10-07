---
name: pa-comfyui
description: Use when working with ComfyUI generation in persona-automation-agent: workflows/registry.json, workflow templates, parameters, models and LoRA, resolution presets, validation limits, or the first real generation.
---

# ComfyUI generation

## Rules

- **Only Registry workflows** (`workflows/registry.json` + `workflows/*.json`). The LLM never writes or selects workflow JSON; templates only contain `{{placeholders}}` that must be `params`, `models`, `inputs` or `filename_prefix` (validated at bridge start).
- Current MVP workflows: `image_generation_v1`, `image_generation_lora_v1`, `image_to_image_v1`. Direction is a **resolution preset**, not a workflow: portrait 4:5 1024×1280, square 1024×1024, landscape 3:2 1536×1024 (TECH_DESIGN 48.3).
- Dynamic values only: `prompt`, `negative_prompt`, `seed`, `width`, `height`, `steps`, `cfg`, `sampler`, `scheduler`, `denoise`, `batch_size`, `checkpoint`, `lora_name`, `lora_strength`. Models are referenced by id and checked against ComfyUI's installed list; paths never come from callers.
- MVP v1.0 operating caps: longest side ≤ 1536, steps ≤ 50, one image, timeout 900 s (`JOB_TIMEOUT_SEC`). ⚙️ The Registry currently allows 2048 / 80 / 4; align it **on the first real generation together with its tests** (`docs/MVP_SCOPE_LOCK.md`, open decision 2).
- Structural changes (nodes added/removed/rewired) get a **new workflow id** (`…_v2`); value-only changes bump `version` (TECH_DESIGN 48.4). Existing Content Jobs keep their workflow.
- Seed `-1` means random; the actual seed is stored in `generation_metadata`.
- OOM: retry once with the same values, then downscale on the third attempt; record `oom_downscaled`.
- Base-model family matters: the installed checkpoint is Flux.1-dev, which needs `cfg = 1` (TECH_DESIGN 48.2). Identity LoRAs are trained per family. The model-family decision is the operator's.
- ComfyUI listens on `127.0.0.1` only.

## First generation

Follow TECH_DESIGN 25.6 and 48.8: manual generation in ComfyUI → record the environment → bridge job without n8n → `ready` + one asset row.

## Personal Edition: GPU-neutral

- Never assume a GPU. Workflows may declare `requirements.min_vram_gb`; the worker refuses ones it cannot run (`WORKFLOW_UNSUPPORTED_ON_TARGET`). This PC is an RTX 3070 8GB (SDXL-class models); large models (e.g. ~16GB `flux1-dev-fp8`) belong on the cloud worker.
- Models and LoRAs are installed per target (`worker_status.models`). The identity LoRA must exist on every target a persona may use; a missing file is `MODEL_NOT_FOUND`/`LORA_NOT_FOUND`, never auto-copied.
