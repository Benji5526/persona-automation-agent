"""Workflow Builder (TECH_DESIGN 13.6 ~ 13.8, 13.12).

값을 정하는 우선순위 (뒤가 앞을 덮어쓴다):
  registry 기본값 → personas.visual_settings.default_params → content_jobs.params → OOM 재시도 조정
모델(checkpoint, LoRA)은 Persona Visual Identity에서, 프롬프트는 content_jobs.prompt 또는 prompt_parts에서 온다.
"""

from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.comfyui.prompt_builder import build_negative, build_prompt
from app.comfyui.registry import PLACEHOLDER_RE, WorkflowSpec
from app.errors import JobError, validation

SEED_MAX = 2**32 - 1
SLOT_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def param_error(message: str) -> JobError:
    return validation("WORKFLOW_PARAM_INVALID", message)


@dataclass
class InputRequest:
    """입력 이미지 자리. Worker가 Storage에서 내려받아 ComfyUI에 올린다."""
    slot: str
    kind: str                 # "asset" | "persona_asset"
    id: str
    row: dict | None = None   # persona_asset이면 여기서 이미 찾은 행


@dataclass
class BuildPlan:
    spec: WorkflowSpec
    values: dict[str, Any]
    inputs: list[InputRequest]
    oom_downscaled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def resolve_workflow_id(job: dict, content_job: dict, persona: dict) -> str | None:
    payload = job.get("payload") or {}
    return (payload.get("workflow") or content_job.get("workflow")
            or (persona.get("visual_settings") or {}).get("default_workflow"))


def _check(name: str, rule: dict, value: Any) -> Any:
    kind = rule.get("type", "float")
    if "enum" in rule:
        if value not in rule["enum"]:
            raise param_error(f"{name} must be one of {rule['enum']}")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise param_error(f"{name} must be a string")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise param_error(f"{name} must be a number")
    if kind == "int":
        if float(value) != int(value):
            raise param_error(f"{name} must be an integer")
        value = int(value)
    if "min" in rule and value < rule["min"]:
        raise param_error(f"{name} must be >= {rule['min']}")
    if "max" in rule and value > rule["max"]:
        raise param_error(f"{name} must be <= {rule['max']}")
    if "multiple_of" in rule and value % rule["multiple_of"] != 0:
        raise param_error(f"{name} must be a multiple of {rule['multiple_of']}")
    return value


def _downscale(spec: WorkflowSpec, values: dict) -> bool:
    """OOM 2차 전략 (13.12): 후보 수를 먼저 줄이고, 1장이면 해상도를 줄인다."""
    if not spec.oom_fallback.get("allow_downscale"):
        return False
    if values.get("batch_size", 1) > 1:
        values["batch_size"] = max(1, values["batch_size"] // 2)
        return True
    if "width" not in values or "height" not in values:
        return False
    step = spec.params.get("width", {}).get("multiple_of", 8)
    min_pixels = spec.oom_fallback.get("min_pixels", 512 * 512)
    w = int(values["width"] * 0.75) // step * step
    h = int(values["height"] * 0.75) // step * step
    if w * h < min_pixels or w < spec.params["width"].get("min", 0) or h < spec.params["height"].get("min", 0):
        return False
    values["width"], values["height"] = w, h
    return True


def plan(spec: WorkflowSpec, job: dict, content_job: dict, persona: dict, persona_assets: list[dict],
         rng: random.Random | None = None) -> BuildPlan:
    rng = rng or random.Random()
    visual = persona.get("visual_settings") or {}

    # 1) Parameter: registry 기본값 → Persona 기본값 → Content Job (registry에 없는 키는 거부)
    values: dict[str, Any] = {k: r["default"] for k, r in spec.params.items() if "default" in r}
    for k, v in (visual.get("default_params") or {}).items():
        if k in spec.params:  # Persona 기본값은 여러 Workflow 공용이라 해당 없는 키는 조용히 무시
            values[k] = v
    if "lora_strength" in visual:
        for k in ("lora_strength_model", "lora_strength_clip"):
            if k in spec.params:
                values[k] = visual["lora_strength"]
    job_params = content_job.get("params") or {}
    unknown = set(job_params) - set(spec.params)
    if unknown:
        raise param_error(f"parameters not allowed for {spec.id}: {sorted(unknown)}")
    values.update(job_params)
    if "batch_size" in spec.params:
        values["batch_size"] = content_job.get("variants") or values.get("batch_size", 1)

    # 2) 프롬프트 (13.7): 직접 쓴 프롬프트가 있으면 그대로, 없으면 prompt_parts를 조립
    prompt = (content_job.get("prompt") or "").strip() or build_prompt(content_job.get("prompt_parts"))
    if "prompt" in spec.params:
        if not prompt and spec.params["prompt"].get("required", True):
            raise validation("PROMPT_MISSING", "no prompt or prompt_parts on the content job")
        values["prompt"] = prompt
    if "negative_prompt" in spec.params:
        values["negative_prompt"] = build_negative(
            (persona.get("content_rules") or {}).get("default_negative_prompt"),
            content_job.get("negative_prompt"),
            (content_job.get("metadata") or {}).get("negative_additions"),
        )

    # 3) seed: -1 또는 없으면 무작위. 실제 값을 기록한다 (13.9)
    if "seed" in spec.params and values.get("seed", -1) in (-1, None):
        values["seed"] = rng.randint(0, SEED_MAX)

    # 4) OOM 2차 재시도: 같은 값으로 한 번 더 실패한 뒤(3번째 시도)에만 줄인다
    oom_downscaled = False
    if job.get("error_code") == "OUT_OF_MEMORY" and int(job.get("attempts") or 0) >= 3:
        oom_downscaled = _downscale(spec, values)

    # 5) 범위 검증
    for name, rule in spec.params.items():
        if name in values:
            values[name] = _check(name, rule, values[name])
        elif rule.get("required"):
            raise param_error(f"{name} is required")

    # 6) 모델: Persona Visual Identity
    by_id = {str(a["id"]): a for a in persona_assets}
    if "checkpoint" in spec.models:
        if not visual.get("base_model"):
            raise validation("MODEL_NOT_FOUND", "persona visual_settings.base_model is not set")
        values["checkpoint"] = visual["base_model"]
    if "lora_name" in spec.models:
        lora = by_id.get(str(visual.get("lora_persona_asset_id")))
        if not lora or lora.get("asset_type") != "lora" or not lora.get("is_active", True):
            raise validation("LORA_NOT_FOUND", "persona has no active LoRA (visual_settings.lora_persona_asset_id)")
        values["lora_name"] = lora["name"]

    # 7) 입력 이미지 자리 (13.6): ID로만 받는다 (URL 금지, 15.8)
    requests: list[InputRequest] = []
    given = content_job.get("input_images") or {}
    unknown_slots = set(given) - set(spec.inputs)
    if unknown_slots:
        raise param_error(f"input slots not allowed for {spec.id}: {sorted(unknown_slots)}")
    for slot, rule in spec.inputs.items():
        ref = given.get(slot)
        if not ref:
            if rule.get("required", True):
                raise validation("INPUT_NOT_FOUND", f"input image {slot} is required")
            continue
        if not SLOT_RE.match(slot) or not isinstance(ref, dict) or len(ref) != 1:
            raise param_error(f"invalid input reference for {slot}")
        kind, ref_id = next(iter(ref.items()))
        kind = {"asset_id": "asset", "persona_asset_id": "persona_asset"}.get(kind)
        try:
            ref_id = str(uuid.UUID(str(ref_id)))
        except ValueError:
            raise param_error(f"{slot} must reference a uuid") from None
        if kind not in rule.get("sources", ["asset", "persona_asset"]):
            raise param_error(f"{slot} does not accept {kind}")
        request = InputRequest(slot=slot, kind=kind, id=str(ref_id))
        if kind == "persona_asset":
            row = by_id.get(str(ref_id))
            if not row or not row.get("storage_path"):
                raise validation("INPUT_NOT_FOUND", f"persona asset {ref_id} not found or has no file")
            allowed = rule.get("asset_types")
            if allowed and row["asset_type"] not in allowed:
                # FaceSwap 원본 얼굴은 Persona의 face_ref만 (15.11)
                raise JobError(f"{slot} must be one of {allowed}", error_type="policy",
                               error_code="POLICY_ERROR", retryable=False)
            request.row = row
        requests.append(request)

    values["filename_prefix"] = f"pa/{job['id']}"
    metadata = {
        "workflow": spec.id, "workflow_version": spec.version,
        "model": values.get("checkpoint"), "lora": values.get("lora_name"),
        "lora_strength": values.get("lora_strength_model"),
        **{k: values[k] for k in ("seed", "steps", "cfg", "width", "height", "denoise", "sampler", "scheduler")
           if k in values},
        "prompt": values.get("prompt"), "negative_prompt": values.get("negative_prompt"),
        "oom_downscaled": oom_downscaled,
    }
    return BuildPlan(spec=spec, values=values, inputs=requests, oom_downscaled=oom_downscaled, metadata=metadata)


def render(template: Any, values: dict[str, Any]) -> Any:
    """자리표시자 채우기. 값 전체가 자리표시자면 원래 타입(int·float)을 유지한다."""
    missing: set[str] = set()

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            whole = PLACEHOLDER_RE.fullmatch(node.strip())
            if whole:
                key = whole.group(1)
                if key in values:
                    return values[key]
                missing.add(key)
                return node

            def sub(m: re.Match[str]) -> str:
                if m.group(1) in values:
                    return str(values[m.group(1)])
                missing.add(m.group(1))
                return m.group(0)

            return PLACEHOLDER_RE.sub(sub, node)
        return node

    result = walk(template)
    if missing:
        raise validation("WORKFLOW_INVALID", f"template placeholders without values: {sorted(missing)}")
    json.dumps(result)  # 직렬화 가능한지 확인
    return result
