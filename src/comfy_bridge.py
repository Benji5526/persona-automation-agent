"""n8n ↔ 로컬 ComfyUI ↔ Supabase 브릿지.

흐름
  1. n8n 이 POST /jobs {"job_id": "<media_queue.id>"} 호출 (job_id 생략 시 다음 대기 작업)
  2. Supabase RPC 로 작업을 선점(pending → processing)하고 즉시 202 응답
  3. 백그라운드 워커가 workflows/<workflow>.json 템플릿에 params 를 채워 ComfyUI 에 전송
  4. 완료되면 결과 파일을 Supabase Storage 에 올리고 media_queue 를 done/failed 로 갱신
  5. (선택) N8N_CALLBACK_URL 로 결과를 알려 다음 단계(SNS 포스팅)를 이어가게 함

GPU 는 하나이므로 워커는 한 번에 한 작업만 처리한다.

실행
  uvicorn src.comfy_bridge:app --host 127.0.0.1 --port 8000
  또는  python src/comfy_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
from supabase import Client, create_client

load_dotenv()

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]

COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "media")
WORKFLOW_DIR = Path(
    os.getenv("WORKFLOW_DIR", Path(__file__).resolve().parent.parent / "workflows")
)
DEFAULT_CHECKPOINT = os.getenv("DEFAULT_CHECKPOINT", "")
JOB_TIMEOUT_SEC = float(os.getenv("JOB_TIMEOUT_SEC", "900"))
POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "1.0"))
RETRY_BASE_DELAY_SEC = float(os.getenv("RETRY_BASE_DELAY_SEC", "60"))  # 재시도 간격: 60s, 120s, 240s ...
N8N_CALLBACK_URL = os.getenv("N8N_CALLBACK_URL", "")
N8N_CALLBACK_TOKEN = os.getenv("N8N_CALLBACK_TOKEN", "")
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8000"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("comfy_bridge")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class JobError(Exception):
    """작업 실패. retryable=True 면 attempts < max_attempts 일 때 pending 으로 되돌려 재시도한다.

    설정/입력 오류(템플릿 없음, 자리표시자 누락, 워크플로우 검증 실패)는 다시 해도 같으므로 False.
    JobError 가 아닌 예외(네트워크, Storage 오류 등)는 일시적일 수 있어 재시도 대상으로 본다.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# 워크플로우 템플릿
#   ComfyUI 에서 "Export (API)" 로 저장한 JSON 에 "{{prompt}}" 같은 자리표시자를 넣어둔다.
#   값 전체가 자리표시자면 원래 타입(int/float) 그대로, 문자열 일부면 문자열로 치환된다.
# ---------------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def render_workflow(node: Any, params: dict[str, Any], missing: set[str]) -> Any:
    if isinstance(node, dict):
        return {k: render_workflow(v, params, missing) for k, v in node.items()}
    if isinstance(node, list):
        return [render_workflow(v, params, missing) for v in node]
    if isinstance(node, str):
        whole = _PLACEHOLDER.fullmatch(node.strip())
        if whole:
            key = whole.group(1)
            if key in params:
                return params[key]
            missing.add(key)
            return node

        def _sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key in params:
                return str(params[key])
            missing.add(key)
            return m.group(0)

        return _PLACEHOLDER.sub(_sub, node)
    return node


def load_workflow(name: str) -> dict[str, Any]:
    # 경로 조작 방지: 이름에는 파일명 문자만 허용
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise JobError(f"잘못된 workflow 이름: {name!r}")
    path = WORKFLOW_DIR / f"{name}.json"
    if not path.is_file():
        raise JobError(f"워크플로우 템플릿 없음: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_params(job: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "seed": random.randint(0, 2**32 - 1),
        "steps": 25,
        "cfg": 7.0,
        "width": 1024,
        "height": 1024,
        "negative_prompt": "",
        "filename_prefix": f"{job['persona_id']}/{job['id']}",
    }
    if DEFAULT_CHECKPOINT:
        defaults["checkpoint"] = DEFAULT_CHECKPOINT
    return {**defaults, **(job.get("params") or {})}


# ---------------------------------------------------------------------------
# ComfyUI API
# ---------------------------------------------------------------------------
async def comfy_upload_input(comfy: httpx.AsyncClient, source: str) -> str:
    """입력 이미지(URL 또는 Storage 경로)를 ComfyUI input 폴더에 올리고 노드에 넣을 이름을 반환."""
    if source.startswith(("http://", "https://")):
        r = await comfy.get(source, follow_redirects=True)
        if r.status_code != 200:
            raise JobError(f"입력 이미지 다운로드 실패 ({r.status_code}): {source}")
        data = r.content
    else:
        data = await asyncio.to_thread(sb.storage.from_(STORAGE_BUCKET).download, source)

    filename = f"{uuid.uuid4().hex}{Path(source.split('?')[0]).suffix or '.png'}"
    r = await comfy.post(
        f"{COMFY_URL}/upload/image",
        files={"image": (filename, data)},
        data={"overwrite": "true"},
    )
    if r.status_code != 200:
        raise JobError(f"ComfyUI 입력 업로드 실패 ({r.status_code}): {r.text[:500]}")
    info = r.json()
    return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]


async def comfy_queue_prompt(comfy: httpx.AsyncClient, workflow: dict[str, Any]) -> str:
    r = await comfy.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": workflow, "client_id": uuid.uuid4().hex},
    )
    if r.status_code != 200:
        # 400 이면 본문에 어떤 노드가 왜 틀렸는지(node_errors) 들어있다
        raise JobError(f"ComfyUI 가 워크플로우를 거부함 ({r.status_code}): {r.text[:2000]}")
    return r.json()["prompt_id"]


def _execution_error(entry: dict[str, Any]) -> str:
    for msg in entry.get("status", {}).get("messages", []):
        if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
            d = msg[1]
            return f"{d.get('node_type')} (node {d.get('node_id')}): {d.get('exception_message', '').strip()}"
    return "ComfyUI 실행 오류 (상세 메시지 없음)"


async def comfy_wait(comfy: httpx.AsyncClient, prompt_id: str) -> dict[str, Any]:
    """/history 에 결과가 나타날 때까지 폴링. 시간 초과 시 작업을 중단시킨다."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOB_TIMEOUT_SEC
    while loop.time() < deadline:
        r = await comfy.get(f"{COMFY_URL}/history/{prompt_id}")
        r.raise_for_status()
        entry = r.json().get(prompt_id)
        if entry:
            if entry.get("status", {}).get("status_str") == "error":
                msg = _execution_error(entry)
                # VRAM 부족은 다른 작업이 끝나면 풀릴 수 있으므로 재시도
                raise JobError(msg, retryable="out of memory" in msg.lower())
            return entry
        await asyncio.sleep(POLL_INTERVAL_SEC)

    # 대기열에 남아 있으면 제거, 실행 중이면 중단 (워커가 1개라 실행 중인 건 이 작업뿐)
    await comfy.post(f"{COMFY_URL}/queue", json={"delete": [prompt_id]})
    await comfy.post(f"{COMFY_URL}/interrupt")
    raise JobError(f"ComfyUI 작업 시간 초과 ({JOB_TIMEOUT_SEC:.0f}s)", retryable=True)


def iter_output_files(entry: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """SaveImage(images), VHS_VideoCombine(gifs) 등 출력 노드의 최종 파일만 골라낸다."""
    for node_output in entry.get("outputs", {}).values():
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item and item.get("type") == "output":
                    yield item


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
def _update_job(job_id: str, fields: dict[str, Any]) -> None:
    sb.table("media_queue").update(fields).eq("id", job_id).execute()


def _upload_to_storage(path: str, data: bytes, content_type: str) -> str:
    bucket = sb.storage.from_(STORAGE_BUCKET)
    bucket.upload(path, data, {"content-type": content_type, "upsert": "true"})
    return bucket.get_public_url(path)


def _claim(job_id: str | None) -> dict[str, Any] | None:
    if job_id:
        res = sb.rpc("claim_media_job", {"p_job_id": job_id}).execute()
    else:
        res = sb.rpc("claim_next_media_job", {}).execute()
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# 작업 처리
# ---------------------------------------------------------------------------
async def process_job(comfy: httpx.AsyncClient, job: dict[str, Any]) -> None:
    job_id = job["id"]
    log.info("job %s 시작 (%s / %s)", job_id, job["job_type"], job["workflow"])
    try:
        params = build_params(job)
        for key, source in (job.get("input_images") or {}).items():
            params[key] = await comfy_upload_input(comfy, source)

        missing: set[str] = set()
        workflow = render_workflow(load_workflow(job["workflow"]), params, missing)
        if missing:
            raise JobError(f"params 에 값이 없는 자리표시자: {sorted(missing)}")

        prompt_id = await comfy_queue_prompt(comfy, workflow)
        await asyncio.to_thread(_update_job, job_id, {"comfy_prompt_id": prompt_id})

        entry = await comfy_wait(comfy, prompt_id)
        files = list(iter_output_files(entry))
        if not files:
            raise JobError("출력 파일이 없음 (워크플로우에 SaveImage/VideoCombine 노드가 있는지 확인)")

        now = datetime.now(timezone.utc)
        paths: list[str] = []
        urls: list[str] = []
        for f in files:
            r = await comfy.get(
                f"{COMFY_URL}/view",
                params={"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": "output"},
            )
            r.raise_for_status()
            path = f"{job['persona_id']}/{job['job_type']}/{now:%Y/%m}/{job_id}/{f['filename']}"
            ctype = mimetypes.guess_type(f["filename"])[0] or "application/octet-stream"
            urls.append(await asyncio.to_thread(_upload_to_storage, path, r.content, ctype))
            paths.append(path)

        result = {
            "status": "done",
            "output_paths": paths,
            "output_urls": urls,
            "params": {**(job.get("params") or {}), "seed": params["seed"]},  # 재현용 seed 기록
            "completed_at": now.isoformat(),
            "error": None,
        }
        await asyncio.to_thread(_update_job, job_id, result)
        log.info("job %s 완료: %d개 파일", job_id, len(urls))
        await notify_n8n({**job, **result})

    except Exception as exc:  # 어떤 오류든 작업은 pending(재시도) 또는 failed 로 남겨야 한다
        if isinstance(exc, JobError):
            msg, retryable = str(exc), exc.retryable
        else:
            msg, retryable = f"{type(exc).__name__}: {exc}", True

        now = datetime.now(timezone.utc)
        attempts = int(job.get("attempts") or 1)
        max_attempts = int(job.get("max_attempts") or 1)
        if retryable and attempts < max_attempts:
            delay = RETRY_BASE_DELAY_SEC * 2 ** (attempts - 1)
            fields = {
                "status": "pending",
                "error": f"[시도 {attempts}/{max_attempts}] {msg}"[:4000],
                "run_after": (now + timedelta(seconds=delay)).isoformat(),
            }
            log.warning("job %s 재시도 예정 (%.0fs 후, %d/%d): %s", job_id, delay, attempts, max_attempts, msg)
        else:
            fields = {"status": "failed", "error": msg[:4000], "completed_at": now.isoformat()}
            log.error("job %s 실패: %s", job_id, msg, exc_info=exc)

        try:
            await asyncio.to_thread(_update_job, job_id, fields)
        except Exception:
            log.exception("job %s 상태 기록 실패", job_id)
        if fields["status"] == "failed":  # 재시도 대기 중에는 n8n 에 알리지 않음
            await notify_n8n({**job, **fields})


async def notify_n8n(job: dict[str, Any]) -> None:
    if not N8N_CALLBACK_URL:
        return
    headers = {"X-Callback-Token": N8N_CALLBACK_TOKEN} if N8N_CALLBACK_TOKEN else {}
    payload = {
        "job_id": job["id"],
        "persona_id": job["persona_id"],
        "job_type": job["job_type"],
        "status": job["status"],
        "attempts": job.get("attempts"),
        "output_urls": job.get("output_urls", []),
        "output_paths": job.get("output_paths", []),
        "error": job.get("error"),
        "source_message_id": job.get("source_message_id"),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(N8N_CALLBACK_URL, json=payload, headers=headers)
            r.raise_for_status()
    except Exception:
        # 콜백 실패해도 DB 상태는 이미 기록됐으므로 n8n 폴링으로 복구 가능
        log.exception("n8n 콜백 실패 (job %s)", job["id"])


async def worker(queue: asyncio.Queue[dict[str, Any]], comfy: httpx.AsyncClient) -> None:
    while True:
        job = await queue.get()
        try:
            await process_job(comfy, job)
        finally:
            queue.task_done()


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.queue = asyncio.Queue()
    app.state.comfy = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
    task = asyncio.create_task(worker(app.state.queue, app.state.comfy))
    log.info("브릿지 시작: ComfyUI=%s, workflows=%s", COMFY_URL, WORKFLOW_DIR)
    try:
        yield
    finally:
        task.cancel()
        await app.state.comfy.aclose()


app = FastAPI(title="ComfyUI Bridge", lifespan=lifespan)


class JobRequest(BaseModel):
    job_id: uuid.UUID | None = None  # 생략하면 가장 우선순위 높은 pending 작업을 가져감


def _check_token(token: str | None) -> None:
    if not token or not secrets.compare_digest(token, BRIDGE_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bridge token")


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(
    req: JobRequest | None = None,
    x_bridge_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_token(x_bridge_token)
    job_id = str(req.job_id) if req and req.job_id else None
    job = await asyncio.to_thread(_claim, job_id)
    if job is None:
        # 없는 ID 이거나 이미 processing/done 인 작업 → n8n 재시도로 인한 중복 방지
        raise HTTPException(status.HTTP_409_CONFLICT, "no claimable pending job")
    queue: asyncio.Queue = app.state.queue
    await queue.put(job)
    return {"accepted": True, "job_id": job["id"], "queue_size": queue.qsize()}


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        r = await app.state.comfy.get(f"{COMFY_URL}/system_stats", timeout=3)
        comfy_ok = r.status_code == 200
    except httpx.HTTPError:
        comfy_ok = False
    return {"ok": True, "comfyui": comfy_ok, "queue_size": app.state.queue.qsize()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT)
