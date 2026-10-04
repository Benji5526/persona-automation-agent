"""현재 브릿지(src/comfy_bridge.py) 테스트: 가짜 ComfyUI·가짜 Supabase.

M2에서 브릿지를 app/ 구조와 automation_jobs 기준으로 바꿀 때 이 테스트도 함께 옮긴다 (TECH_DESIGN 16.7).
실행:  .venv/Scripts/python -m pytest tests/bridge -q
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cb():
    os.environ.update(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="k" * 40,
        BRIDGE_TOKEN="tok",
        DEFAULT_CHECKPOINT="sdxl.safetensors",
        POLL_INTERVAL_SEC="0.01",
        RETRY_BASE_DELAY_SEC="60",
    )
    sys.path.insert(0, str(ROOT))
    module = importlib.import_module("src.comfy_bridge")
    yield module
    sys.path.remove(str(ROOT))


@pytest.fixture
def fake_db(cb, monkeypatch):
    """Supabase 호출을 기록만 하는 가짜로 바꾼다."""
    calls = {"updates": [], "uploads": [], "callbacks": []}
    monkeypatch.setattr(cb, "_update_job", lambda job_id, fields: calls["updates"].append(fields))

    def upload(path, data, ctype):
        calls["uploads"].append((path, len(data), ctype))
        return f"https://public/{path}"

    monkeypatch.setattr(cb, "_upload_to_storage", upload)

    async def notify(job):
        calls["callbacks"].append(job["status"])

    monkeypatch.setattr(cb, "notify_n8n", notify)
    return calls


def make_comfy(mode="success"):
    """mode: success | timeout | oom | conn | exec_error"""
    state = {"polls": 0, "sent": None}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "face.png", "subfolder": "", "type": "input"})
        if path == "/prompt":
            if mode == "conn":
                raise httpx.ConnectError("refused")
            state["sent"] = json.loads(req.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "p1", "number": 0, "node_errors": {}})
        if path == "/history/p1":
            if mode == "oom":
                return httpx.Response(200, json={"p1": {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"node_id": "3", "node_type": "KSampler", "exception_message": "CUDA out of memory"}]]}}})
            if mode == "exec_error":
                return httpx.Response(200, json={"p1": {"status": {"status_str": "error", "messages": [
                    ["execution_start", {}],
                    ["execution_error", {"node_id": "4", "node_type": "CheckpointLoaderSimple", "exception_message": "file not found"}]]}}})
            if mode == "timeout":
                return httpx.Response(200, json={})
            state["polls"] += 1
            if state["polls"] < 3:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"p1": {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {
                    "9": {"images": [{"filename": "a_00001_.png", "subfolder": "mina", "type": "output"}]},
                    "10": {"images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}]},
                }}})
        if path == "/view":
            assert req.url.params["subfolder"] == "mina"
            return httpx.Response(200, content=b"PNGDATA")
        if path == "/img.png":
            return httpx.Response(200, content=b"src")
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), state


JOB = {
    "id": "11111111-1111-1111-1111-111111111111",
    "persona_id": "mina",
    "job_type": "image",
    "workflow": "txt2img_basic",
    "params": {"prompt": "hello", "width": 832},
    "input_images": {},
}


# -----------------------------------------------------------------------------
# 정상 흐름
# -----------------------------------------------------------------------------
def test_success_renders_template_and_uploads_only_final_outputs(cb, fake_db):
    comfy, state = make_comfy()
    asyncio.run(cb.process_job(comfy, JOB))
    wf = state["sent"]
    assert isinstance(wf["3"]["inputs"]["seed"], int)  # 값 전체가 자리표시자면 타입 유지
    assert wf["5"]["inputs"]["width"] == 832
    assert wf["4"]["inputs"]["ckpt_name"] == "sdxl.safetensors"
    assert wf["6"]["inputs"]["text"] == "hello"
    assert len(fake_db["uploads"]) == 1  # temp 미리보기는 올리지 않음
    path, size, ctype = fake_db["uploads"][0]
    assert path.endswith(f"{JOB['id']}/a_00001_.png") and size == 7 and ctype == "image/png"
    final = fake_db["updates"][-1]
    assert final["status"] == "done" and final["output_urls"][0].startswith("https://public/")
    assert fake_db["callbacks"] == ["done"]


def test_input_image_is_uploaded_to_comfy(cb):
    comfy, _ = make_comfy()
    assert asyncio.run(cb.comfy_upload_input(comfy, "http://x/img.png")) == "face.png"


def test_execution_error_message_is_extracted(cb):
    comfy, _ = make_comfy("exec_error")
    with pytest.raises(cb.JobError) as exc:
        asyncio.run(cb.comfy_wait(comfy, "p1"))
    assert str(exc.value) == "CheckpointLoaderSimple (node 4): file not found"


# -----------------------------------------------------------------------------
# 재시도 판단
# -----------------------------------------------------------------------------
def run(cb, fake_db, mode, attempts, **overrides):
    fake_db["updates"].clear()
    fake_db["callbacks"].clear()
    comfy, _ = make_comfy(mode)
    job = {**JOB, "params": {"prompt": "x", "checkpoint": "c"}, "attempts": attempts, "max_attempts": 3, **overrides}
    asyncio.run(cb.process_job(comfy, job))
    return fake_db["updates"][-1]


def seconds_until(iso: str) -> float:
    return (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()


def test_timeout_retries_with_exponential_backoff(cb, fake_db, monkeypatch):
    monkeypatch.setattr(cb, "JOB_TIMEOUT_SEC", 0.05)
    first = run(cb, fake_db, "timeout", 1)
    assert first["status"] == "pending" and "[시도 1/3]" in first["error"] and not fake_db["callbacks"]
    assert 55 < seconds_until(first["run_after"]) <= 60
    second = run(cb, fake_db, "timeout", 2)
    assert 115 < seconds_until(second["run_after"]) <= 120


def test_last_attempt_fails_and_notifies(cb, fake_db, monkeypatch):
    monkeypatch.setattr(cb, "JOB_TIMEOUT_SEC", 0.05)
    final = run(cb, fake_db, "timeout", 3)
    assert final["status"] == "failed" and fake_db["callbacks"] == ["failed"]


@pytest.mark.parametrize("mode", ["oom", "conn"])
def test_transient_errors_are_retried(cb, fake_db, mode):
    assert run(cb, fake_db, mode, 1)["status"] == "pending"


def test_missing_placeholder_is_not_retried(cb, fake_db, monkeypatch):
    monkeypatch.setattr(cb, "DEFAULT_CHECKPOINT", "")
    final = run(cb, fake_db, "success", 1, params={})
    assert final["status"] == "failed"
    assert "checkpoint" in final["error"] and "prompt" in final["error"]


@pytest.mark.parametrize("workflow", ["nope", "../etc"])
def test_bad_workflow_name_is_not_retried(cb, fake_db, workflow):
    assert run(cb, fake_db, "success", 1, workflow=workflow)["status"] == "failed"


# -----------------------------------------------------------------------------
# HTTP API
# -----------------------------------------------------------------------------
def test_jobs_endpoint_auth_claim_and_conflict(cb, monkeypatch):
    from fastapi.testclient import TestClient

    claims = []

    def claim(job_id):
        claims.append(job_id)
        if job_id == "22222222-2222-2222-2222-222222222222":
            return None
        return {**JOB, "id": job_id or "next"}

    async def idle_worker(queue, comfy):
        await asyncio.Event().wait()

    monkeypatch.setattr(cb, "_claim", claim)
    monkeypatch.setattr(cb, "worker", idle_worker)

    with TestClient(cb.app) as client:
        assert client.post("/jobs", json={"job_id": JOB["id"]}).status_code == 401
        assert client.post("/jobs", json={"job_id": JOB["id"]}, headers={"X-Bridge-Token": "wrong"}).status_code == 401
        ok = client.post("/jobs", json={"job_id": JOB["id"]}, headers={"X-Bridge-Token": "tok"})
        assert ok.status_code == 202 and ok.json()["job_id"] == JOB["id"]
        assert client.post("/jobs", headers={"X-Bridge-Token": "tok"}).json()["job_id"] == "next"
        conflict = client.post("/jobs", json={"job_id": "22222222-2222-2222-2222-222222222222"},
                               headers={"X-Bridge-Token": "tok"})
        assert conflict.status_code == 409
        health = client.get("/health").json()
        assert health["ok"] is True and "comfyui" in health
    assert claims == [JOB["id"], None, "22222222-2222-2222-2222-222222222222"]
