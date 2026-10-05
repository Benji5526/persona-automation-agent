"""브릿지 시작점.

실행:  .venv/Scripts/python -m app.main
       (또는 uvicorn "app.main:create_app" --factory --host 127.0.0.1 --port 8000)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI

from app import __version__
from app.api import router
from app.comfyui.client import ComfyClient
from app.comfyui.registry import load_registry
from app.config import Settings
from app.database import PostgrestRepository, Repository
from app.security import AuthFailureTracker, RateLimiter, RedactingFilter
from app.storage import Storage, SupabaseStorage
from app.worker import GpuWorker, Notifier

log = logging.getLogger("bridge")


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactingFilter((settings.supabase_secret_key, *settings.bridge_tokens, settings.n8n_callback_token))
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


def create_app(settings: Settings | None = None, *, repo: Repository | None = None, storage: Storage | None = None,
               comfy: ComfyClient | None = None, notifier: Notifier | None = None,
               start_background: bool = True) -> FastAPI:
    """의존성을 주입할 수 있게 만든다 (테스트는 가짜 ComfyUI·실제 DB를 넣는다)."""
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
        setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        clients: list[httpx.AsyncClient] = []

        def new_client(**kwargs) -> httpx.AsyncClient:
            client = httpx.AsyncClient(**kwargs)
            clients.append(client)
            return client

        state = app.state
        state.settings = settings
        state.repo = repo or PostgrestRepository(new_client(timeout=30), settings.supabase_url, settings.supabase_secret_key)
        state.storage = storage or SupabaseStorage(new_client(timeout=120), settings.supabase_url, settings.supabase_secret_key)
        state.comfy = comfy or ComfyClient(new_client(timeout=httpx.Timeout(60.0, connect=5.0)), settings.comfy_url,
                                           settings.object_info_cache_sec)
        state.notifier = notifier or Notifier(new_client(timeout=15), settings.n8n_callback_url, settings.n8n_callback_token)
        state.registry = load_registry(settings.workflow_dir)
        state.worker = GpuWorker(settings, state.repo, state.storage, state.comfy, state.registry, state.notifier)
        state.auth_tracker = AuthFailureTracker(settings.auth_fail_limit, settings.auth_block_sec)
        state.rate_limiter = RateLimiter(settings.jobs_rate_per_sec)
        state.comfy_check = None
        state.auth_logged = set()
        log.info("bridge %s starting: worker=%s comfy=%s workflows=%s", __version__, settings.worker_id,
                 settings.comfy_url, sorted(state.registry))

        tasks: list[asyncio.Task] = []
        state.worker.require_loop = start_background
        if start_background:
            state.worker.loop_task = asyncio.create_task(state.worker.run_forever(), name="gpu-worker")
            tasks.append(state.worker.loop_task)
            tasks.append(asyncio.create_task(state.worker.heartbeat_loop(), name="heartbeat"))
            tasks.append(asyncio.create_task(state.worker.report_loop(), name="status-report"))
        try:
            yield
        finally:
            # 실행 중·대기 중 Job을 재시도 대기로 돌려놓고 끝낸다 (회수를 기다리지 않게)
            await state.worker.shutdown()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for client in clients:
                await client.aclose()

    app = FastAPI(title="persona-automation-agent bridge", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)  # 공개 문서 페이지를 열지 않는다
    app.include_router(router)
    return app


if __name__ == "__main__":
    import uvicorn

    load_dotenv()
    _settings = Settings.from_env()
    setup_logging(_settings)
    uvicorn.run(create_app(_settings), host=_settings.host, port=_settings.port, log_level=_settings.log_level.lower())
