"""브릿지 Worker 통합 테스트: 실제 DB(마이그레이션) + 가짜 ComfyUI (TECH_DESIGN 13, 16.13)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from tests.bridge.conftest import FakeComfy, make_settings, noise_png_sized


def job_row(env, job_id):
    return env.seed.as_postgres().one("select * from automation_jobs where id = %s", (job_id,))


def content_status(env, content_job_id):
    return env.seed.as_postgres().one("select status from content_jobs where id = %s", (content_job_id,))["status"]


# -----------------------------------------------------------------------------
# 정상 흐름
# -----------------------------------------------------------------------------
def test_lora_generation_end_to_end(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"], p_variants=2)

    assert env.process(ids["job_id"]) is not None

    # ComfyUI로 보낸 Workflow: Persona Visual Identity + Prompt Builder + Persona 기본 파라미터
    wf = env.comfy.prompts[0]
    assert wf["4"]["inputs"]["ckpt_name"] == "model_a.safetensors"
    assert wf["10"]["inputs"]["lora_name"] == "gina_v3.safetensors"
    assert wf["10"]["inputs"]["strength_model"] == 0.75
    assert wf["6"]["inputs"]["text"] == "Gina, Tokyo at night, taking a photo, neon, photorealistic"
    assert wf["7"]["inputs"]["text"] == "lowres, blurry, extra fingers"  # 중복(Blurry) 제거
    assert wf["5"]["inputs"] == {"width": 832, "height": 1216, "batch_size": 2}
    assert isinstance(wf["3"]["inputs"]["seed"], int) and wf["3"]["inputs"]["steps"] == 25
    assert wf["9"]["inputs"]["filename_prefix"] == f"pa/{ids['job_id']}"

    # DB: Job done, Asset 2개, Content Job ready (Rollup R1)
    job = job_row(env, ids["job_id"])
    assert job["status"] == "done" and len(job["result"]["asset_ids"]) == 2
    assets = env.seed.all("select * from assets where automation_job_id = %s order by created_at", (ids["job_id"],))
    assert len(assets) == 2
    for a in assets:
        assert a["storage_path"] == f"persona/{persona['id']}/assets/{a['id']}.png"
        assert (a["width"], a["height"], a["mime_type"]) == (832, 1216, "image/png")
        assert a["generation_metadata"]["workflow"] == "image_generation_lora_v1"
        assert a["generation_metadata"]["lora"] == "gina_v3.safetensors"
        assert a["thumbnail_url"].endswith(f"{a['id']}_thumb.webp")
        assert ("media", a["storage_path"]) in env.storage.objects
    assert content_status(env, ids["content_job_id"]) == "ready"

    # 실행 기록 (17.7 진행 단계)
    steps = [(r["step"], r["status"]) for r in env.seed.all(
        "select step, status from execution_logs where automation_job_id = %s order by id", (ids["job_id"],))]
    assert steps == [("BUILD", "started"), ("BUILD", "succeeded"), ("COMFYUI_QUEUE", "started"),
                     ("COMFYUI_WAIT", "started"), ("COMFYUI_WAIT", "succeeded"), ("VALIDATE", "started"),
                     ("VALIDATE", "succeeded"), ("UPLOAD", "started"), ("UPLOAD", "succeeded"),
                     ("COMPLETE", "succeeded")]
    # 단계별 소요 시간 (49.5 2번): 검증·업로드·ComfyUI·전체가 모두 기록된다
    durations = {r["step"]: r["duration_ms"] for r in env.seed.all(
        "select step, duration_ms from execution_logs where automation_job_id = %s and status = 'succeeded'",
        (ids["job_id"],))}
    for step in ("COMFYUI_WAIT", "VALIDATE", "UPLOAD", "COMPLETE"):
        assert durations[step] is not None and durations[step] >= 0, step
    assert env.seed.one("select actor_type from state_transitions where entity_id = %s and to_status = 'done'",
                        (ids["job_id"],))["actor_type"] == "python"

    # n8n 콜백 (12.7)
    assert env.notifier.events == [{"event": "generation.completed", "job_id": ids["job_id"],
                                    "content_job_id": ids["content_job_id"], "persona_id": persona["id"],
                                    "status": "done", "attempts": 1, "asset_ids": job["result"]["asset_ids"],
                                    "error": None}]


def test_image_to_image_uses_persona_reference(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ref = env.seed.face_ref(uid, persona["id"], env.storage, noise_png_sized(600, 600, 3))
    ids = env.seed.generation_job(uid, persona["id"], p_workflow="image_to_image_v1", p_prompt="Gina in Seoul",
                                  p_input_images={"init_image": {"persona_asset_id": ref}})
    env.process(ids["job_id"])
    wf = env.comfy.prompts[0]
    assert wf["11"]["inputs"]["image"] in env.comfy.uploads
    assert wf["12"]["inputs"]["width"] == 832 and wf["3"]["inputs"]["denoise"] == 0.55
    assert job_row(env, ids["job_id"])["status"] == "done"


# -----------------------------------------------------------------------------
# 실행 전 검증 실패: 재시도 없이 failed (13.10)
# -----------------------------------------------------------------------------
def test_missing_lora_file_fails_before_comfyui(env):
    env.comfy.loras = ["other.safetensors"]
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    env.process(ids["job_id"])
    job = job_row(env, ids["job_id"])
    assert (job["status"], job["error_type"], job["error_code"]) == ("failed", "validation", "LORA_NOT_FOUND")
    assert env.comfy.prompts == []  # ComfyUI를 부르지 않았다
    assert content_status(env, ids["content_job_id"]) == "failed"
    assert env.notifier.events[-1]["event"] == "generation.failed"
    assert env.notifier.events[-1]["error"]["code"] == "LORA_NOT_FOUND"


def test_unknown_parameter_is_rejected(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"], p_params={"denoise": 0.3})  # T2I+LoRA에는 없는 키
    env.process(ids["job_id"])
    assert job_row(env, ids["job_id"])["error_code"] == "WORKFLOW_PARAM_INVALID"


def test_disabled_workflow_is_rejected(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"], p_workflow="faceswap_v1")
    env.process(ids["job_id"])
    job = job_row(env, ids["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "WORKFLOW_INVALID")


def test_invalid_input_file_is_rejected(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ref = env.seed.face_ref(uid, persona["id"], env.storage, b"MZ\x90\x00 this is not an image")
    ids = env.seed.generation_job(uid, persona["id"], p_workflow="image_to_image_v1", p_prompt="x",
                                  p_input_images={"init_image": {"persona_asset_id": ref}})
    env.process(ids["job_id"])
    assert job_row(env, ids["job_id"])["error_code"] == "INPUT_NOT_FOUND"
    assert env.comfy.uploads == {}


# -----------------------------------------------------------------------------
# 재시도 가능한 오류 (13.12, 14.11)
# -----------------------------------------------------------------------------
def test_oom_retries_same_values_then_downscales(env):
    env.comfy.modes = ["oom", "oom", "success"]
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"], p_variants=2)

    env.process(ids["job_id"])
    first = job_row(env, ids["job_id"])
    assert (first["status"], first["error_code"]) == ("pending", "OUT_OF_MEMORY")
    wait = (datetime.fromisoformat(first["run_after"]) - datetime.now(timezone.utc)).total_seconds()
    assert 20 < wait <= 31  # 첫 재시도 30초

    env.seed.requeue_now(ids["job_id"])
    env.process(ids["job_id"])                     # 2번째: 같은 값으로
    assert env.comfy.prompts[1]["5"]["inputs"]["batch_size"] == 2
    env.seed.requeue_now(ids["job_id"])
    env.process(ids["job_id"])                     # 3번째: 후보 수를 줄여서
    assert env.comfy.prompts[2]["5"]["inputs"]["batch_size"] == 1
    job = job_row(env, ids["job_id"])
    assert job["status"] == "done" and job["result"]["oom_downscaled"] is True
    meta = env.seed.one("select generation_metadata from assets where automation_job_id = %s", (ids["job_id"],))
    assert meta["generation_metadata"]["oom_downscaled"] is True


def test_tiny_output_is_retryable_output_invalid(env):
    env.comfy.modes = ["tiny"]
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    env.process(ids["job_id"])
    job = job_row(env, ids["job_id"])
    assert (job["status"], job["error_code"]) == ("pending", "OUTPUT_INVALID")
    assert env.seed.one("select count(*) as n from assets")["n"] == 0


def test_wrong_output_size_is_rejected(env):
    env.comfy.image_size = (512, 512)
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    env.process(ids["job_id"])
    assert job_row(env, ids["job_id"])["error_code"] == "OUTPUT_INVALID"


def test_timeout_cancels_comfy_and_retries(env):
    env.comfy.modes = ["never"]
    env.settings = make_settings(job_timeout_sec=0.2)
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    env.process(ids["job_id"])
    job = job_row(env, ids["job_id"])
    assert (job["status"], job["error_code"]) == ("pending", "TIMEOUT")
    assert env.comfy.cancelled == env.comfy.prompt_ids and env.comfy.interrupts == 1  # 이 작업만 정리


def test_node_error_is_not_retried(env):
    env.comfy.modes = ["node_error"]
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    env.process(ids["job_id"])
    job = job_row(env, ids["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "NODE_ERROR")


# -----------------------------------------------------------------------------
# 잠금 상실 (11.6): 취소되면 Heartbeat가 false → 즉시 멈추고 결과를 버린다
# -----------------------------------------------------------------------------
def test_cancel_while_generating_stops_worker(env):
    env.comfy.modes = ["never"]
    env.settings = make_settings(heartbeat_sec=0.05, job_timeout_sec=30)
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])

    async def scenario():
        worker = env.worker()
        job = await env.repo.claim_job(ids["job_id"], "python:test")
        runner = asyncio.create_task(worker.run_forever())
        worker.enqueue(job)
        while not env.comfy.prompts:  # ComfyUI에 요청이 들어간 뒤(생성 중)에 취소한다
            await asyncio.sleep(0.01)
        await asyncio.to_thread(lambda: env.seed.as_operator(uid).one(
            "select status from cancel_content_job(%s)", (ids["content_job_id"],)))
        for _ in range(200):
            if worker.current is None:
                break
            await asyncio.sleep(0.02)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        return worker

    worker = env.run(scenario())
    assert worker.current is None
    assert env.comfy.interrupts >= 1                       # ComfyUI 실행 중단
    assert job_row(env, ids["job_id"])["status"] == "cancelled"
    assert env.seed.one("select count(*) as n from assets")["n"] == 0
    assert env.notifier.events == []                       # 잠금을 잃은 Job은 알리지 않는다


# -----------------------------------------------------------------------------
# 노드 확인·상태 보고 (17.4)
# -----------------------------------------------------------------------------
def test_refresh_nodes_disables_workflows_and_syncs(env):
    env.comfy.missing_nodes = {"ImageScale"}
    worker = env.worker()
    enabled = env.run(worker.refresh_nodes())
    assert enabled["image_to_image_v1"] is False and enabled["image_generation_v1"] is True
    assert enabled["faceswap_v1"] is False  # registry에서 꺼짐
    rows = {r["id"]: r["enabled"] for r in env.seed.as_postgres().all("select id, enabled from comfy_workflows")}
    assert rows["image_to_image_v1"] is False and rows["image_generation_lora_v1"] is True


def test_status_report(env):
    worker = env.worker()

    async def go():
        info = await worker.status_info()
        await env.repo.report_worker_status("python:rtx5080-1", info)
        return info

    info = env.run(go())
    assert info["comfyui_ok"] is True and info["gpu"]["vram_free_mb"] == 2048
    # 설치된 모델 목록 (0008, 22.9): 예전 형식(체크포인트)과 COMBO 형식(LoRA) 모두 읽는다
    models = {"checkpoints": ["model_a.safetensors"], "loras": ["gina_v3.safetensors"]}
    assert info["models"] == models
    row = env.seed.as_postgres().one("select comfyui_ok, gpu, models from worker_status where id = 'python:rtx5080-1'")
    assert row["comfyui_ok"] is True and "RTX 5080" in row["gpu"]["name"] and row["models"] == models

    # ComfyUI가 꺼지면 목록을 보내지 않고, DB는 이전 목록을 유지한다
    env.comfy.reachable = False
    info = env.run(go())
    assert info["comfyui_ok"] is False and "models" not in info
    row = env.seed.as_postgres().one("select comfyui_ok, models from worker_status where id = 'python:rtx5080-1'")
    assert row == {"comfyui_ok": False, "models": models}


# -----------------------------------------------------------------------------
# 코드 리뷰 회귀 테스트 (M2)
# -----------------------------------------------------------------------------
def test_queued_jobs_get_heartbeats_and_lost_ones_are_dropped(env):
    """H1: 대기 중인 Job도 Heartbeat를 받고, 잠금을 잃으면 대기열에서 빠진다."""
    env.settings = make_settings(heartbeat_sec=0.05)
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    a = env.seed.generation_job(uid, persona["id"])
    b = env.seed.generation_job(uid, persona["id"])

    async def scenario():
        worker = env.worker()
        job_a = await env.repo.claim_job(a["job_id"], "python:test")
        job_b = await env.repo.claim_job(b["job_id"], "python:test")
        worker.current = job_a           # A가 실행 중인 것처럼
        worker.enqueue(job_b)            # B는 대기
        hb = asyncio.create_task(worker.heartbeat_loop())
        before = job_row(env, b["job_id"])["heartbeat_at"]
        after = before
        for _ in range(250):  # 고정 대기 대신 조건이 될 때까지 (최대 5초, 부하가 있어도 안정적으로)
            await asyncio.sleep(0.02)
            after = await asyncio.to_thread(lambda: job_row(env, b["job_id"])["heartbeat_at"])
            if after > before:
                break
        await asyncio.to_thread(lambda: env.seed.as_operator(uid).one(
            "select status from cancel_content_job(%s)", (b["content_job_id"],)))
        for _ in range(250):
            if not worker.pending:
                break
            await asyncio.sleep(0.02)
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)
        return before, after, worker.queued_ids

    before, after, queued = env.run(scenario())
    assert after > before          # 대기 중에도 Heartbeat가 갱신됐다
    assert queued == []            # 취소돼 잠금을 잃은 대기 Job은 빠졌다


def test_worker_loop_survives_unexpected_errors(env):
    """H2: 실패 보고 중 예외가 나도 루프는 살아서 다음 Job을 처리한다."""
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    bad = env.seed.generation_job(uid, persona["id"])
    good = env.seed.generation_job(uid, persona["id"])

    async def scenario():
        worker = env.worker()
        original_fail = env.repo.fail

        async def broken_fail(*args, **kwargs):
            raise RuntimeError("proxy returned html")

        env.repo.fail = broken_fail
        worker.loop_task = asyncio.create_task(worker.run_forever())
        job_bad = await env.repo.claim_job(bad["job_id"], "python:test")
        job_bad["content_job_id"] = "00000000-0000-0000-0000-000000000000"  # 실패하도록
        worker.enqueue(job_bad)
        worker.enqueue(await env.repo.claim_job(good["job_id"], "python:test"))
        for _ in range(300):
            if not worker.pending and worker.current is None and job_row(env, good["job_id"])["status"] == "done":
                break
            await asyncio.sleep(0.02)
        healthy = worker.healthy()
        env.repo.fail = original_fail
        worker.loop_task.cancel()
        await asyncio.gather(worker.loop_task, return_exceptions=True)
        return healthy

    assert env.run(scenario()) is True
    assert job_row(env, good["job_id"])["status"] == "done"


def test_shutdown_returns_jobs_for_retry(env):
    """M2: 종료할 때 실행 중·대기 중 Job을 재시도 대기로 돌려놓는다."""
    env.comfy.modes = ["never"]
    env.settings = make_settings(job_timeout_sec=30)
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    a = env.seed.generation_job(uid, persona["id"])
    b = env.seed.generation_job(uid, persona["id"])

    async def scenario():
        worker = env.worker()
        worker.loop_task = asyncio.create_task(worker.run_forever())
        worker.enqueue(await env.repo.claim_job(a["job_id"], "python:test"))
        worker.enqueue(await env.repo.claim_job(b["job_id"], "python:test"))
        while not env.comfy.prompts:
            await asyncio.sleep(0.01)
        await worker.shutdown()
        worker.loop_task.cancel()
        await asyncio.gather(worker.loop_task, return_exceptions=True)
        return worker

    worker = env.run(scenario())
    assert not worker.healthy()
    for ids in (a, b):
        job = job_row(env, ids["job_id"])
        assert (job["status"], job["error_code"]) == ("pending", "SHUTDOWN")
    assert env.comfy.cancelled == env.comfy.prompt_ids  # ComfyUI에 남은 작업도 지웠다


def test_requeued_same_id_runs_with_new_lock(env):
    """M3: 같은 id가 다시 선점돼도 예전 항목(옛 locked_at)이 실행되지 않는다."""
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])

    async def scenario():
        worker = env.worker()
        stale = await env.repo.claim_job(ids["job_id"], "python:test")
        worker.enqueue(stale)
        await worker.cancel(ids["job_id"])                       # 대기 중 취소 → 빠짐
        await env.repo.fail(ids["job_id"], stale["locked_at"], "transient", "X", "x", True)
        await asyncio.to_thread(env.seed.requeue_now, ids["job_id"])
        fresh = await env.repo.claim_job(ids["job_id"], "python:test")
        worker.enqueue(fresh)
        return worker.pending

    pending = env.run(scenario())
    assert len(pending) == 1 and pending[0]["attempts"] == 2


# -----------------------------------------------------------------------------
# F0 보강 (TECH_DESIGN 36.12, 47.3, 49.5)
# -----------------------------------------------------------------------------
def test_content_job_of_another_persona_fails_before_comfyui(env):
    """36.12: Job과 Content Job의 Persona가 다르면 ComfyUI를 부르기 전에 실패한다 (DB 트리거 밖의 방어선)."""
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    other = env.seed.persona(env.seed.operator())      # 실제로 있는 다른 Persona (존재하지 않는 ID가 아님)
    ids = env.seed.generation_job(uid, persona["id"])

    async def go():
        worker = env.worker()
        job = await env.repo.claim_job(ids["job_id"], env.settings.worker_id)
        job["persona_id"] = other["id"]  # 선점한 Job dict의 persona가 Content Job과 어긋난 상황
        await worker.process(job)

    env.run(go())
    assert env.comfy.prompts == []
    row = job_row(env, ids["job_id"])
    assert row["error_code"] == "INPUT_NOT_FOUND"
    assert "different persona" in row["error_message"]   # persona 조회 실패가 아니라 새 검사에 걸렸다


def test_unexpected_output_names_are_rejected_without_reading_files(env):
    """47.3 2번: 이름 끝 표기·허용 폴더 밖 파일은 /view를 한 번도 부르지 않고 실패한다 (재시도 없음)."""
    for mode in ("bad_name", "bad_folder"):
        env.comfy = FakeComfy(modes=[mode])
        uid = env.seed.operator()
        persona = env.seed.persona(uid)
        ids = env.seed.generation_job(uid, persona["id"])
        env.process(ids["job_id"])
        row = job_row(env, ids["job_id"])
        assert (row["status"], row["error_code"]) == ("failed", "OUTPUT_UNEXPECTED"), mode
        assert env.comfy.views == [], mode
        assert env.seed.one("select count(*) as n from assets where automation_job_id = %s",
                            (ids["job_id"],))["n"] == 0
    event = env.seed.one("select actor_type, detail from security_events where event_type = 'OUTPUT_UNEXPECTED'"
                         " order by id desc limit 1")
    assert event["actor_type"] == "python" and event["detail"]["job_id"]
    assert env.seed.one("select persona_id::text as p from security_events where event_type = 'OUTPUT_UNEXPECTED'"
                        " order by id desc limit 1")["p"] == persona["id"]  # Operator가 자기 화면에서 볼 수 있다


def test_oversized_output_is_not_retried(env):
    """47.3 3번: Registry output.max_bytes를 넘으면 업로드 전에 OUTPUT_TOO_LARGE, 재시도 없음."""
    env.registry["image_generation_lora_v1"].output["max_bytes"] = 20000
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    env.process(ids["job_id"])
    row = job_row(env, ids["job_id"])
    assert (row["status"], row["error_code"]) == ("failed", "OUTPUT_TOO_LARGE")
    assert env.storage.objects == {}


def test_job_with_registered_asset_is_completed_without_regenerating(env):
    """49.5 1번: register_asset 뒤 complete 전에 죽은 Job이 Heartbeat 회수로 다시 선점돼도
    ComfyUI를 부르지 않고 Asset도 늘지 않는다 (후보 2개 중 1개만 등록된 상태도 완료로 본다, 11.4)."""
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"], p_variants=2)
    asset_id = str(uuid.uuid4())

    async def crash_after_register():
        job = await env.repo.claim_job(ids["job_id"], env.settings.worker_id)
        row = await env.repo.register_asset(job["id"], job["locked_at"], {
            "id": asset_id, "asset_type": "image", "file_name": f"{asset_id}.png", "mime_type": "image/png",
            "storage_path": f"persona/{persona['id']}/assets/{asset_id}.png", "width": 832, "height": 1216,
            "public_url": "https://example.supabase.co/x.png", "generation_metadata": {"seed": 1}})
        assert row is not None   # 여기서 브릿지가 죽었다고 보고 complete는 부르지 않는다
    env.run(crash_after_register())

    # 실제 회수 경로: Heartbeat가 끊긴 Job을 recover_stale_jobs()가 pending으로 되돌린다 (pg_cron이 부르는 함수)
    env.seed.as_postgres()
    env.seed.conn.execute("update automation_jobs set heartbeat_at = now() - interval '1 hour' where id = %s",
                          (ids["job_id"],))
    assert env.seed.one("select recover_stale_jobs() as n")["n"] == 1
    assert job_row(env, ids["job_id"])["status"] == "pending"
    env.seed.requeue_now(ids["job_id"])               # 백오프 대기를 건너뛴다

    assert env.process(ids["job_id"]) is not None
    assert env.comfy.prompts == []                    # 다시 생성하지 않았다
    assets = env.seed.all("select id from assets where automation_job_id = %s", (ids["job_id"],))
    assert [a["id"] for a in assets] == [asset_id]
    row = job_row(env, ids["job_id"])
    assert row["status"] == "done" and row["result"]["recovered"] is True and row["result"]["asset_ids"] == [asset_id]
    assert content_status(env, ids["content_job_id"]) == "ready"
    assert ("RECOVERED", "succeeded") in [(r["step"], r["status"]) for r in env.seed.all(
        "select step, status from execution_logs where automation_job_id = %s", (ids["job_id"],))]


# -----------------------------------------------------------------------------
# Pull 방식 (TECH_DESIGN 56.3): 놀고 있을 때 DB에서 Job을 가져간다
# -----------------------------------------------------------------------------
def run_pull_loop(env, job_id, seconds=4.0):
    """Pull 루프와 실행 루프를 잠시 돌리고, Job이 끝나면(또는 시간이 다 되면) 멈춘다."""
    env.settings = make_settings(pull_jobs=True, pull_interval_sec=0.01)

    async def go():
        worker = env.worker()
        worker.loop_task = asyncio.create_task(worker.run_forever())
        pull = asyncio.create_task(worker.pull_loop())
        try:
            for _ in range(int(seconds / 0.05)):
                await asyncio.sleep(0.05)
                if job_row(env, job_id)["status"] in ("done", "failed"):
                    break
        finally:
            pull.cancel()
            worker.loop_task.cancel()
            await asyncio.gather(pull, worker.loop_task, return_exceptions=True)

    env.run(go())


def set_active_worker(env, worker_id):
    env.seed.as_postgres()
    if worker_id is None:
        env.seed.conn.execute("update app_settings set value = 'null'::jsonb where key = 'active_worker'")
    else:
        env.seed.conn.execute("update app_settings set value = to_jsonb(%s::text) where key = 'active_worker'",
                              (worker_id,))


def test_pull_loop_claims_and_generates_without_any_request(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    run_pull_loop(env, ids["job_id"])
    row = job_row(env, ids["job_id"])
    assert row["status"] == "done" and row["claimed_by"] == env.settings.worker_id
    assert content_status(env, ids["content_job_id"]) == "ready"


def test_pull_loop_only_takes_jobs_while_this_worker_is_active(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])

    set_active_worker(env, "python:cloud-1")                     # 다른 GPU가 활성
    run_pull_loop(env, ids["job_id"], seconds=0.5)
    row = job_row(env, ids["job_id"])
    assert (row["status"], row["attempts"]) == ("pending", 0) and env.comfy.prompts == []

    set_active_worker(env, env.settings.worker_id)               # 이 GPU로 전환
    run_pull_loop(env, ids["job_id"])
    assert job_row(env, ids["job_id"])["status"] == "done"


def test_pull_loop_does_not_claim_while_comfyui_is_down(env):
    env.comfy.reachable = False
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    run_pull_loop(env, ids["job_id"], seconds=0.5)
    row = job_row(env, ids["job_id"])
    assert (row["status"], row["attempts"]) == ("pending", 0)    # 선점하지 않아 attempts가 늘지 않는다
