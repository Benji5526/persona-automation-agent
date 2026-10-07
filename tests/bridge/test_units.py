"""브릿지 단위 테스트: 조립·검증·보안·Supabase 요청 형식."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path

import httpx
import pytest

from app.comfyui.builder import plan, render
from app.comfyui.prompt_builder import build_negative, build_prompt
from app.comfyui.registry import RegistryError, load_registry
from app.comfyui.validation import combo_options, make_thumbnail, validate_input_image, validate_output
from app.config import ConfigError
from app.database import PostgrestRepository
from app.errors import DbError, JobError
from app.security import AuthFailureTracker, RateLimiter, RedactingFilter, redact, token_matches
from app.storage import SupabaseStorage, safe_path
from tests.bridge.conftest import ROOT, make_settings, noise_png_sized

REGISTRY = load_registry(ROOT / "workflows")
PERSONA = {"visual_settings": {"base_model": "m.safetensors", "lora_persona_asset_id": "11111111-1111-4111-8111-111111111111", "lora_strength": 0.6,
                               "default_params": {"width": 832, "steps": 20, "unknown_for_this_workflow": 1}},
           "content_rules": {"default_negative_prompt": "lowres"}}
ASSETS = [{"id": "11111111-1111-4111-8111-111111111111", "asset_type": "lora", "name": "gina.safetensors", "is_active": True},
          {"id": "22222222-2222-4222-8222-222222222222", "asset_type": "face_ref", "name": "f", "storage_path": "persona/p/refs/f.png", "is_active": True},
          {"id": "33333333-3333-4333-8333-333333333333", "asset_type": "style_ref", "name": "s", "storage_path": "persona/p/refs/s.png", "is_active": True}]


def job(**kw):
    return {"id": "job-1", "attempts": 1, "payload": {}, **kw}


def content(**kw):
    return {"prompt": None, "prompt_parts": {"subject": "Gina", "style": "photo"}, "params": {}, "variants": 1,
            "input_images": {}, "metadata": {}, **kw}


# -- Prompt Builder (13.7) ------------------------------------------------------
def test_prompt_builder_order_and_whitespace():
    parts = {"style": "photorealistic", "subject": " Gina ", "location": "Tokyo\nat night", "unknown": "x", "mood": ""}
    assert build_prompt(parts) == "Gina, Tokyo at night, photorealistic"
    assert build_prompt(None) == ""


def test_negative_merges_and_dedupes():
    assert build_negative("lowres, blurry", None, ["Blurry", "extra fingers"]) == "lowres, blurry, extra fingers"


# -- Builder (13.6) -------------------------------------------------------------
def test_plan_merges_values_in_priority_order():
    spec = REGISTRY["image_generation_lora_v1"]
    p = plan(spec, job(), content(params={"steps": 40}, variants=3), PERSONA, ASSETS, random.Random(1))
    v = p.values
    assert (v["width"], v["height"], v["steps"], v["batch_size"]) == (832, 1024, 40, 3)  # registry < persona < job
    assert v["lora_name"] == "gina.safetensors" and v["lora_strength_model"] == 0.6
    assert v["prompt"] == "Gina, photo" and v["negative_prompt"] == "lowres"
    assert 0 <= v["seed"] <= 2**32 - 1


def test_plan_prefers_direct_prompt_and_keeps_explicit_seed():
    p = plan(REGISTRY["image_generation_v1"], job(), content(prompt="hello", params={"seed": 42}), PERSONA, ASSETS)
    assert p.values["prompt"] == "hello" and p.values["seed"] == 42


@pytest.mark.parametrize("params,code", [
    ({"width": 1004}, "WORKFLOW_PARAM_INVALID"),       # multiple_of 8
    ({"width": 4096}, "WORKFLOW_PARAM_INVALID"),       # max
    ({"steps": 1.5}, "WORKFLOW_PARAM_INVALID"),        # int
    ({"sampler": "magic"}, "WORKFLOW_PARAM_INVALID"),  # enum
    ({"cfg": "7"}, "WORKFLOW_PARAM_INVALID"),          # 숫자 아님
    ({"denoise": 0.5}, "WORKFLOW_PARAM_INVALID"),      # 이 Workflow에 없는 키
])
def test_plan_rejects_bad_params(params, code):
    with pytest.raises(JobError) as exc:
        plan(REGISTRY["image_generation_v1"], job(), content(params=params), PERSONA, ASSETS)
    assert exc.value.error_code == code and not exc.value.retryable


def test_plan_requires_prompt_and_models():
    with pytest.raises(JobError, match="prompt") as exc:
        plan(REGISTRY["image_generation_v1"], job(), content(prompt_parts=None), PERSONA, ASSETS)
    assert exc.value.error_code == "PROMPT_MISSING"
    with pytest.raises(JobError) as exc:
        plan(REGISTRY["image_generation_lora_v1"], job(), content(), {"visual_settings": {"base_model": "m"}}, [])
    assert exc.value.error_code == "LORA_NOT_FOUND"


def test_oom_downscale_only_on_third_attempt():
    spec = REGISTRY["image_generation_v1"]
    big = content(variants=1, params={"width": 1024, "height": 1536})
    second = plan(spec, job(attempts=2, error_code="OUT_OF_MEMORY"), big, PERSONA, ASSETS)
    assert not second.oom_downscaled and second.values["width"] == 1024
    third = plan(spec, job(attempts=3, error_code="OUT_OF_MEMORY"), big, PERSONA, ASSETS)
    assert third.oom_downscaled and (third.values["width"], third.values["height"]) == (768, 1152)
    # 최소 픽셀(1024x768) 아래로는 줄이지 않는다 (832x1024 → 624x768은 너무 작음)
    small = plan(spec, job(attempts=3, error_code="OUT_OF_MEMORY"), content(variants=1), PERSONA, ASSETS)
    assert not small.oom_downscaled and small.values["width"] == 832


def test_input_slots_by_id_and_faceswap_policy():
    spec = REGISTRY["image_to_image_v1"]
    with pytest.raises(JobError) as exc:
        plan(spec, job(), content(), PERSONA, ASSETS)
    assert exc.value.error_code == "INPUT_NOT_FOUND"
    p = plan(spec, job(), content(input_images={"init_image": {"asset_id": "99999999-9999-4999-8999-999999999999"}}), PERSONA, ASSETS)
    assert [(r.slot, r.kind, r.id) for r in p.inputs] == [("init_image", "asset", "99999999-9999-4999-8999-999999999999")]
    with pytest.raises(JobError):  # uuid가 아닌 참조는 거부
        plan(spec, job(), content(input_images={"init_image": {"asset_id": "../../etc"}}), PERSONA, ASSETS)
    with pytest.raises(JobError):  # URL은 받지 않는다 (SSRF 방지)
        plan(spec, job(), content(input_images={"init_image": {"url": "http://169.254.169.254/"}}), PERSONA, ASSETS)

    faceswap = REGISTRY["faceswap_v1"]
    with pytest.raises(JobError) as exc:  # 원본 얼굴은 face_ref만 (15.11)
        plan(faceswap, job(), content(input_images={"target_image": {"asset_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                                                    "source_face": {"persona_asset_id": "33333333-3333-4333-8333-333333333333"}}), PERSONA, ASSETS)
    assert exc.value.error_code == "POLICY_ERROR"


def test_render_keeps_types_and_reports_missing():
    out = render({"a": "{{n}}", "b": "x {{n}} y", "c": ["{{s}}"]}, {"n": 3, "s": "z"})
    assert out == {"a": 3, "b": "x 3 y", "c": ["z"]}
    with pytest.raises(JobError) as exc:
        render({"a": "{{missing}}"}, {})
    assert exc.value.error_code == "WORKFLOW_INVALID"


# -- Registry -------------------------------------------------------------------
def test_registry_rejects_undeclared_placeholder(tmp_path: Path):
    (tmp_path / "w.json").write_text(json.dumps({"1": {"class_type": "X", "inputs": {"a": "{{surprise}}"}}}))
    (tmp_path / "registry.json").write_text(json.dumps({"w": {"version": "1", "type": "image", "file": "w.json",
                                                             "output": {"mime": ["image/png"]}}}))
    with pytest.raises(RegistryError, match="surprise"):
        load_registry(tmp_path)


def test_registry_rejects_path_in_file_name(tmp_path: Path):
    (tmp_path / "registry.json").write_text(json.dumps({"w": {"version": "1", "type": "image",
                                                             "file": "../secrets.json", "output": {"mime": ["x"]}}}))
    with pytest.raises(RegistryError):
        load_registry(tmp_path)


# -- 실행 전·후 검증 -------------------------------------------------------------
def test_combo_options_supports_both_formats():
    assert combo_options({"N": {"input": {"required": {"x": [["a", "b"]]}}}}, "N", "x") == ["a", "b"]
    assert combo_options({"N": {"input": {"required": {"x": ["COMBO", {"options": ["c"]}]}}}}, "N", "x") == ["c"]
    assert combo_options({}, "N", "x") is None


def test_validate_output():
    spec = REGISTRY["image_generation_v1"]
    data = noise_png_sized(832, 1216, 0)
    info = validate_output(data, spec, 832, 1216)
    assert (info.mime, info.width, info.height) == ("image/png", 832, 1216)
    for bad, w, h in [(data, 1024, 1024), (b"x" * 20000, 832, 1216), (b"tiny", 832, 1216)]:
        with pytest.raises(JobError) as exc:
            validate_output(bad, spec, w, h)
        assert exc.value.error_code == "OUTPUT_INVALID" and exc.value.retryable


def test_input_image_checked_by_content_not_extension():
    assert validate_input_image(noise_png_sized(64, 64, 1)) == "png"
    with pytest.raises(JobError):
        validate_input_image(b"MZ\x90\x00 pretend.png")


def test_thumbnail_is_small_webp():
    thumb = make_thumbnail(noise_png_sized(832, 1216, 0))
    assert thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP"


# -- 보안 (15.6, 15.8, 15.17, 15.21) -----------------------------------------------
def test_token_matching_and_config_rules():
    assert token_matches("a" * 40, ("b" * 40, "a" * 40))
    assert not token_matches(None, ("a" * 40,)) and not token_matches("", ("a" * 40,))
    with pytest.raises(ConfigError):
        make_settings(bridge_tokens=("short",))
    with pytest.raises(ConfigError):
        make_settings(comfy_url="http://0.0.0.0:8188")


def test_failure_tracker_blocks_and_expires():
    now = [0.0]
    tracker = AuthFailureTracker(limit=3, block_sec=10, clock=lambda: now[0])
    assert [tracker.record_failure("1.1.1.1") for _ in range(4)] == [False, False, False, True]  # 3회를 넘으면
    assert tracker.is_blocked("1.1.1.1") and not tracker.is_blocked("2.2.2.2")
    now[0] = 11
    assert not tracker.is_blocked("1.1.1.1")


def test_rate_limiter():
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert [limiter.allow() for _ in range(3)] == [True, True, False]
    now[0] = 1
    assert limiter.allow()


def test_redaction():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    text = f"Authorization: Bearer {jwt} apikey=sb_secret_abc123 X-Bridge-Token: 'tok123' extra"
    out = redact(text, ("extra",))
    assert jwt not in out and "sb_secret_abc123" not in out and "tok123" not in out and "extra" not in out

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "key %s", ("sb_secret_zzz",), None)
    RedactingFilter().filter(record)
    assert "sb_secret_zzz" not in record.getMessage()


def test_storage_paths_are_safe():
    assert safe_path("persona/p/assets/a.png") == "persona/p/assets/a.png"
    for bad in ("../x", "persona/../x", "/abs", "a//b", "a\\b", ""):
        with pytest.raises(ValueError):
            safe_path(bad)


# -- Supabase 요청 형식 (PostgREST·Storage) -----------------------------------------
def test_postgrest_requests_and_errors():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/rpc/claim_automation_job"):
            return httpx.Response(200, json=[{"id": "j1", "locked_at": "2026-10-05T00:00:00.123456+00:00"}])
        if request.url.path.endswith("/rpc/heartbeat_automation_job"):
            return httpx.Response(200, json=False)
        if request.url.path.endswith("/rpc/register_asset"):
            return httpx.Response(200, json=[])
        return httpx.Response(409, json={"code": "PT409", "message": "INVALID_TRANSITION", "details": "x"})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        repo = PostgrestRepository(client, "https://ref.supabase.co", "sb_secret_abc")
        job = await repo.claim_job("j1", "python:1")
        alive = await repo.heartbeat("j1", job["locked_at"])
        asset = await repo.register_asset("j1", job["locked_at"], {})
        with pytest.raises(DbError) as exc:
            await repo.complete("j1", job["locked_at"], {})
        legacy = PostgrestRepository(client, "https://ref.supabase.co", "a.b.c")
        await legacy.claim_job("j1", "python:1")
        return job, alive, asset, exc.value

    job, alive, asset, err = asyncio.run(go())
    assert job["id"] == "j1" and alive is False and asset is None
    assert (err.status, err.code, err.message) == (409, "PT409", "INVALID_TRANSITION")
    first = seen[0]
    assert str(first.url) == "https://ref.supabase.co/rest/v1/rpc/claim_automation_job"
    assert first.headers["apikey"] == "sb_secret_abc" and first.headers["x-actor"] == "python"
    assert "authorization" not in first.headers                      # 새 secret key는 apikey만
    assert json.loads(seen[1].content)["p_locked_at"] == "2026-10-05T00:00:00.123456+00:00"  # 받은 값 그대로
    assert seen[-1].headers["authorization"] == "Bearer a.b.c"       # 레거시 JWT 키


def test_storage_requests():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404 if request.method == "GET" else 200, json={})

    async def go():
        storage = SupabaseStorage(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                  "https://ref.supabase.co", "sb_secret_abc")
        await storage.upload("media", "persona/p/assets/a.png", b"x", "image/png")
        with pytest.raises(JobError) as exc:
            await storage.download("persona-private", "persona/p/refs/missing.png")
        return storage, exc.value

    storage, err = asyncio.run(go())
    assert str(seen[0].url) == "https://ref.supabase.co/storage/v1/object/media/persona/p/assets/a.png"
    assert seen[0].headers["x-upsert"] == "true" and seen[0].headers["content-type"] == "image/png"
    assert err.error_code == "INPUT_NOT_FOUND"
    assert storage.public_url("media", "persona/p/assets/a.png") == \
        "https://ref.supabase.co/storage/v1/object/public/media/persona/p/assets/a.png"


# -----------------------------------------------------------------------------
# F0: 출력 파일 이름 허용 목록 (TECH_DESIGN 47.3 2번), 크기 상한 (47.3 3번)
# -----------------------------------------------------------------------------
JOB = "11111111-2222-3333-4444-555555555555"


def test_output_names_allow_list():
    from app.comfyui.validation import check_output_names
    spec = REGISTRY["image_generation_v1"]
    ok = [{"filename": f"{JOB}_00001_.png", "subfolder": "pa", "type": "output"},
          {"filename": f"{JOB}_00002_.png", "subfolder": "pa", "type": "output"}]
    check_output_names(ok, JOB, spec)  # 통과

    bad_cases = [
        {"filename": f"{JOB}_00001_.png [input]", "subfolder": "pa"},        # 폴더를 바꾸는 이름 끝 표기
        {"filename": f"{JOB}_00001_.png", "subfolder": "pa/../x"},           # 하위·상위 폴더
        {"filename": f"{JOB}_00001_.png", "subfolder": ""},                  # 허용 폴더(pa) 밖
        {"filename": f"{JOB}_00001_.jpg", "subfolder": "pa"},                # Registry 형식(png) 밖
        {"filename": "22222222-2222-2222-2222-222222222222_00001_.png", "subfolder": "pa"},  # 다른 Job
        {"filename": f"../{JOB}_00001_.png", "subfolder": "pa"},
        {"filename": f"{JOB}_1_.png", "subfolder": "pa"},                    # 자리수
        {"filename": f"{JOB}_\u0661\u0662\u0663\u0664\u0665_.png", "subfolder": "pa"},  # ASCII가 아닌 숫자
    ]
    for bad in bad_cases:
        with pytest.raises(JobError) as exc:
            check_output_names([ok[0], bad], JOB, spec)  # 하나라도 다르면 전체 거부
        assert exc.value.error_code == "OUTPUT_UNEXPECTED" and exc.value.retryable is False, bad


def test_output_size_cap_is_checked_before_decoding():
    spec = REGISTRY["image_generation_v1"]
    data = noise_png_sized(256, 256, 1)
    spec.output["max_bytes"] = len(data) - 1
    try:
        with pytest.raises(JobError) as exc:
            validate_output(data, spec, 256, 256)
        assert exc.value.error_code == "OUTPUT_TOO_LARGE" and exc.value.retryable is False
    finally:
        spec.output["max_bytes"] = 52428800
    assert validate_output(data, spec, 256, 256).width == 256


def test_registry_rejects_max_bytes_above_bucket_limit(tmp_path):
    import shutil
    for name in ("registry.json", "image_generation_v1.json", "image_generation_lora_v1.json", "image_to_image_v1.json",
                 "character_reference_v1.json", "faceswap_v1.json"):
        shutil.copy(ROOT / "workflows" / name, tmp_path / name)
    reg = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    reg["image_generation_v1"]["output"]["max_bytes"] = 52428801     # media 버킷 한도(50MB)보다 큼
    (tmp_path / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    with pytest.raises(RegistryError, match="max_bytes"):
        load_registry(tmp_path)
