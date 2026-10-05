"""브릿지 테스트 도구: 실제 DB(마이그레이션 적용) + 가짜 ComfyUI + 메모리 Storage.

* PsycopgRepository: app.database.Repository를 psycopg로 구현한다. PostgREST 대신 같은 SQL 함수를
  service_role + x-actor: python으로 직접 부른다. 반환 값은 PostgREST처럼 JSON 직렬화 형태(문자열 시각·uuid)로 바꾼다.
* FakeComfy: httpx.MockTransport로 ComfyUI API를 흉내 낸다.
* Seed: Operator·n8n 입장에서 데이터를 만든다 (커밋됨).
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from PIL import Image
from psycopg.rows import dict_row

from app.comfyui.client import ComfyClient
from app.comfyui.registry import load_registry
from app.config import Settings
from app.errors import DbError, validation
from app.worker import GpuWorker

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40


def jsonable(row: Any) -> Any:
    return json.loads(json.dumps(row, default=str)) if row is not None else None


# -----------------------------------------------------------------------------
# Repository (psycopg)
# -----------------------------------------------------------------------------
class PsycopgRepository:
    def __init__(self, url: str):
        self.url = url

    def _call_sync(self, sql: str, params: Any) -> list:
        # Windows의 기본 이벤트 루프에서는 async psycopg를 쓸 수 없어 동기 연결을 스레드에서 쓴다
        with psycopg.connect(self.url, autocommit=True, row_factory=dict_row) as conn:
            conn.execute("set role service_role")
            conn.execute("select set_config('request.headers', %s, false)", (json.dumps({"x-actor": "python"}),))
            try:
                cur = conn.execute(sql, params)
                return cur.fetchall() if cur.description else []
            except psycopg.Error as exc:
                raise DbError(400, exc.sqlstate, str(exc).splitlines()[0]) from exc

    async def _call(self, sql: str, params: Any = None, many: bool = False) -> Any:
        rows = jsonable(await asyncio.to_thread(self._call_sync, sql, params))
        return rows if many else (rows[0] if rows else None)

    async def _scalar(self, sql: str, params: Any = None) -> Any:
        row = await self._call(sql, params)
        return next(iter(row.values())) if row else None

    async def claim_job(self, job_id, worker):
        return await self._call("select * from claim_automation_job(%s, %s)", (job_id, worker))

    async def claim_next_job(self, job_type, worker):
        return await self._call("select * from claim_next_automation_job(%s, %s)", (job_type, worker))

    async def get_job(self, job_id):
        return await self._call("select * from automation_jobs where id = %s", (job_id,))

    async def heartbeat(self, job_id, locked_at):
        return bool(await self._scalar("select heartbeat_automation_job(%s, %s) as v", (job_id, locked_at)))

    async def complete(self, job_id, locked_at, result):
        return bool(await self._scalar("select complete_automation_job(%s, %s, %s::jsonb) as v",
                                       (job_id, locked_at, json.dumps(result))))

    async def fail(self, job_id, locked_at, error_type, error_code, message, retryable, step=None,
                   retry_after_seconds=None):
        return await self._scalar("select fail_automation_job(%s, %s, %s, %s, %s, %s, %s, %s) as v",
                                  (job_id, locked_at, error_type, error_code, message, retryable,
                                   retry_after_seconds, step))

    async def log(self, job_id, step, service, status, *, input=None, output=None, duration_ms=None,
                  execution_ref=None, error=None):
        await self._call("select log_execution(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
                         (job_id, step, service, status, json.dumps(input) if input is not None else None,
                          json.dumps(output) if output is not None else None, duration_ms, execution_ref, error))

    async def register_asset(self, job_id, locked_at, asset):
        return await self._call("select * from register_asset(%s, %s, %s::jsonb)", (job_id, locked_at, json.dumps(asset)))

    async def get_content_job(self, content_job_id):
        return await self._call("select * from content_jobs where id = %s", (content_job_id,))

    async def get_persona(self, persona_id):
        return await self._call("select * from personas where id = %s", (persona_id,))

    async def get_persona_assets(self, persona_id):
        return await self._call("select * from persona_assets where persona_id = %s and is_active", (persona_id,), many=True)

    async def get_asset(self, asset_id):
        return await self._call("select * from assets where id = %s", (asset_id,))

    async def sync_workflow_registry(self, workflows):
        return int(await self._scalar("select sync_workflow_registry(%s::jsonb) as v", (json.dumps(workflows),)))

    async def report_worker_status(self, worker_id, info):
        await self._call("select report_worker_status(%s, 'python', %s::jsonb)", (worker_id, json.dumps(info)))

    async def log_security_event(self, event_type, source_ip, detail):
        await self._call("select log_security_event(%s, 'anonymous', %s, %s::jsonb)",
                         (event_type, source_ip, json.dumps(detail)))


# -----------------------------------------------------------------------------
# Storage (메모리)
# -----------------------------------------------------------------------------
class MemoryStorage:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    async def upload(self, bucket, path, data, content_type):
        self.objects[(bucket, path)] = (data, content_type)

    async def download(self, bucket, path):
        if (bucket, path) not in self.objects:
            raise validation("INPUT_NOT_FOUND", f"storage object not found: {bucket}/{path}")
        return self.objects[(bucket, path)][0]

    def public_url(self, bucket, path):
        return f"https://example.supabase.co/storage/v1/object/public/{bucket}/{path}"


def noise_png(width: int, height: int, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    img = Image.frombytes("RGB", (width, height), bytes(rng.getrandbits(8) for _ in range(width * height * 3)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# -----------------------------------------------------------------------------
# ComfyUI (가짜)
# -----------------------------------------------------------------------------
@dataclass
class FakeComfy:
    """modes: 시도마다 하나씩 꺼내 쓰는 동작 목록 (success, oom, node_error, no_output, tiny, never)."""
    modes: list[str] = field(default_factory=lambda: ["success"])
    checkpoints: list[str] = field(default_factory=lambda: ["model_a.safetensors"])
    loras: list[str] = field(default_factory=lambda: ["gina_v3.safetensors"])
    missing_nodes: set[str] = field(default_factory=set)
    reachable: bool = True
    image_size: tuple[int, int] | None = None   # None이면 요청 크기로 만든다
    prompts: list[dict] = field(default_factory=list)
    prompt_ids: list[str] = field(default_factory=list)
    uploads: dict[str, bytes] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    interrupts: int = 0
    _outputs: dict[str, bytes] = field(default_factory=dict)
    _history: dict[str, dict] = field(default_factory=dict)

    NODES = ["CheckpointLoaderSimple", "LoraLoader", "EmptyLatentImage", "CLIPTextEncode", "KSampler",
             "VAEDecode", "SaveImage", "LoadImage", "ImageScale", "VAEEncode"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("connection refused")
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"name": "cuda:0 NVIDIA GeForce RTX 5080",
                                                         "vram_total": 16 * 2**30, "vram_free": 2 * 2**30}]})
        if path == "/object_info":
            info = {n: {"input": {"required": {}}} for n in self.NODES if n not in self.missing_nodes}
            if "CheckpointLoaderSimple" in info:
                info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [self.checkpoints]
            if "LoraLoader" in info:  # 새 COMBO 형식도 지원하는지 확인
                info["LoraLoader"]["input"]["required"]["lora_name"] = ["COMBO", {"options": self.loras}]
            return httpx.Response(200, json=info)
        if path == "/upload/image":
            name = f"up_{len(self.uploads)}.png"
            body = request.read()
            self.uploads[name] = body
            return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
        if path == "/prompt":
            body = json.loads(request.read())
            workflow = body["prompt"]
            self.prompts.append(workflow)
            prompt_id = body.get("prompt_id") or f"p{len(self.prompts)}"
            self.prompt_ids.append(prompt_id)
            mode = self.modes.pop(0) if self.modes else "success"
            self._history[prompt_id] = self._result(prompt_id, workflow, mode)
            return httpx.Response(200, json={"prompt_id": prompt_id, "number": 0, "node_errors": {}})
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[1]
            entry = self._history.get(prompt_id)
            return httpx.Response(200, json={prompt_id: entry} if entry else {})
        if path == "/view":
            return httpx.Response(200, content=self._outputs[request.url.params["filename"]])
        if path == "/queue" and request.method == "GET":
            running = [[0, pid, {}, {}, []] for pid, entry in self._history.items() if entry is None]
            return httpx.Response(200, json={"queue_running": running[:1], "queue_pending": running[1:]})
        if path == "/queue":
            deleted = json.loads(request.read()).get("delete", [])
            self.cancelled.extend(deleted)
            return httpx.Response(200, json={})
        if path == "/interrupt":
            self.interrupts += 1
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def _result(self, prompt_id: str, workflow: dict, mode: str) -> dict | None:
        if mode == "never":
            return None
        if mode in ("oom", "node_error"):
            message = "CUDA out of memory. Tried to allocate 2.00 GiB" if mode == "oom" else "Error while deserializing"
            return {"status": {"status_str": "error", "messages": [
                ["execution_error", {"node_id": "3", "node_type": "KSampler", "exception_message": message}]]}}
        outputs: list[dict] = []
        if mode != "no_output":
            latent = next((n["inputs"] for n in workflow.values() if n["class_type"] in ("EmptyLatentImage", "ImageScale")), {})
            w, h = self.image_size or (latent.get("width", 1024), latent.get("height", 1024))
            for i in range(latent.get("batch_size", 1) or 1):
                name = f"{prompt_id}_{i:05d}_.png"
                self._outputs[name] = b"tiny" if mode == "tiny" else noise_png_sized(w, h, i)
                outputs.append({"filename": name, "subfolder": "pa", "type": "output"})
        return {"status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"9": {"images": outputs}, "99": {"images": [{"filename": "x.png", "subfolder": "", "type": "temp"}]}}}

    def client(self) -> ComfyClient:
        return ComfyClient(httpx.AsyncClient(transport=httpx.MockTransport(self.handler)), "http://127.0.0.1:8188")


_PNG_CACHE: dict[tuple[int, int, int], bytes] = {}


def noise_png_sized(width: int, height: int, seed: int) -> bytes:
    """요청 크기의 PNG. 큰 노이즈 이미지는 느리므로 작은 노이즈를 확대해 만든다 (10KB 이상)."""
    key = (width, height, seed)
    if key not in _PNG_CACHE:
        small = Image.open(io.BytesIO(noise_png(64, 64, seed)))
        out = io.BytesIO()
        small.resize((width, height), Image.NEAREST).save(out, format="PNG")
        _PNG_CACHE[key] = out.getvalue()
    return _PNG_CACHE[key]


class RecordingNotifier:
    def __init__(self):
        self.events: list[dict] = []

    async def send(self, payload: dict) -> None:
        self.events.append(payload)


# -----------------------------------------------------------------------------
# 데이터 준비 (Operator·n8n 입장, 커밋됨)
# -----------------------------------------------------------------------------
class Seed:
    def __init__(self, url: str):
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)

    def close(self):
        self.conn.close()

    def _as(self, role: str, claims: dict | None = None, actor: str | None = None):
        self.conn.execute("reset role")
        self.conn.execute("select set_config('request.jwt.claims', %s, false)", (json.dumps(claims) if claims else "",))
        self.conn.execute("select set_config('request.headers', %s, false)",
                          (json.dumps({"x-actor": actor}) if actor else "",))
        if role != "postgres":
            self.conn.execute(f"set role {role}")

    def one(self, sql: str, params: Any = None) -> dict | None:
        return jsonable(self.conn.execute(sql, params).fetchone())

    def all(self, sql: str, params: Any = None) -> list[dict]:
        return jsonable(self.conn.execute(sql, params).fetchall())

    def as_postgres(self):
        self._as("postgres")
        return self

    def as_operator(self, uid: str):
        self._as("authenticated", {"sub": uid, "role": "authenticated"})
        return self

    def as_n8n(self):
        self._as("service_role", actor="n8n")
        return self

    def operator(self) -> str:
        self.as_postgres()
        email = f"op-{uuid.uuid4().hex[:8]}@example.com"
        self.conn.execute("update app_settings set value = value || to_jsonb(%s::text) where key = 'allowed_emails'", (email,))
        return self.one("insert into auth.users (email) values (%s) returning id::text as id", (email,))["id"]

    def persona(self, uid: str, *, with_lora: bool = True, workflow: str = "image_generation_lora_v1") -> dict:
        self.as_operator(uid)
        pid = self.one("insert into personas (name, slug, content_rules, visual_settings)"
                       " values ('Gina', %s, %s, '{}') returning id::text as id",
                       (f"gina-{uuid.uuid4().hex[:6]}", json.dumps({"default_negative_prompt": "lowres, blurry"})))["id"]
        visual = {"default_workflow": workflow, "base_model": "model_a.safetensors",
                  "default_params": {"width": 832, "height": 1216, "steps": 25}, "lora_strength": 0.75}
        lora_id = None
        if with_lora:
            lora_id = self.one("insert into persona_assets (persona_id, asset_type, name)"
                               " values (%s, 'lora', 'gina_v3.safetensors') returning id::text as id", (pid,))["id"]
            visual["lora_persona_asset_id"] = lora_id
        self.conn.execute("update personas set visual_settings = %s where id = %s", (json.dumps(visual), pid))
        return {"id": pid, "lora_id": lora_id}

    def face_ref(self, uid: str, persona_id: str, storage: MemoryStorage, data: bytes, asset_type: str = "face_ref") -> str:
        path = f"persona/{persona_id}/refs/{uuid.uuid4().hex}.png"
        storage.objects[("persona-private", path)] = (data, "image/png")
        self.as_operator(uid)
        return self.one("insert into persona_assets (persona_id, asset_type, name, storage_path)"
                        " values (%s, %s, 'face.png', %s) returning id::text as id", (persona_id, asset_type, path))["id"]

    def generation_job(self, uid: str, persona_id: str, *, max_attempts: int = 3, **job_args: Any) -> dict:
        """Operator가 Content Job을 만들고, n8n이 선점한 뒤 generation Job을 만든 상태."""
        self.as_operator(uid)
        args = {"p_persona_id": persona_id, "p_content_type": "image", "p_topic": "도쿄 야경", **job_args}
        names = ", ".join(f"{k} => %({k})s" for k in args)
        cj = self.one(f"select id::text as id from create_content_job({names})",
                      {k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in args.items()})
        self.as_n8n()
        self.one("select * from claim_content_job(%s)", (cj["id"],))
        if "p_prompt" not in job_args:
            parts = {"subject": "Gina", "location": "Tokyo at night", "action": "taking a photo",
                     "style": "photorealistic", "lighting": "neon"}
            self.conn.execute("update content_jobs set prompt_parts = %s, metadata = %s where id = %s",
                              (json.dumps(parts), json.dumps({"negative_additions": ["extra fingers", "Blurry"]}), cj["id"]))
        job = self.one("select * from create_automation_job('generation', %s, %s, p_max_attempts => %s,"
                       " p_idempotency_key => %s)", (persona_id, cj["id"], max_attempts, f"generation:{cj['id']}:1"))
        return {"content_job_id": cj["id"], "job_id": job["id"]}

    def requeue_now(self, job_id: str) -> None:
        self.as_postgres()
        self.conn.execute("update automation_jobs set run_after = now() - interval '1 second' where id = %s", (job_id,))


@dataclass
class Env:
    url: str
    seed: Seed
    repo: PsycopgRepository
    storage: MemoryStorage
    comfy: FakeComfy
    notifier: RecordingNotifier
    settings: Settings
    registry: dict

    def worker(self) -> GpuWorker:
        return GpuWorker(self.settings, self.repo, self.storage, self.comfy.client(), self.registry,
                         self.notifier, rng=random.Random(7))

    def run(self, coro):
        return asyncio.run(coro)

    def process(self, job_id: str) -> dict | None:
        """선점하고 처리한다. 선점에 실패하면 None."""
        async def go():
            worker = self.worker()
            job = await self.repo.claim_job(job_id, self.settings.worker_id)
            if job:
                await worker.process(job)
            return job
        return self.run(go())


def make_settings(**overrides: Any) -> Settings:
    base = dict(supabase_url="https://example.supabase.co", supabase_secret_key="sb_secret_test",
                bridge_tokens=(TOKEN,), workflow_dir=ROOT / "workflows", poll_interval_sec=0.01,
                job_timeout_sec=2.0, heartbeat_sec=30.0, comfy_check_cache_sec=0.0)
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def env(fresh_database_url):
    seed = Seed(fresh_database_url)
    try:
        yield Env(url=fresh_database_url, seed=seed, repo=PsycopgRepository(fresh_database_url),
                  storage=MemoryStorage(), comfy=FakeComfy(), notifier=RecordingNotifier(),
                  settings=make_settings(), registry=load_registry(ROOT / "workflows"))
    finally:
        seed.close()
