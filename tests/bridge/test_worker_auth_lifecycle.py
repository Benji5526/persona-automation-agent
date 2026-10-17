"""Worker 인증 생애주기 (TECH_DESIGN 56.5, 사양 21조): 자격이 거부되면 정상 종료한다.

  * worker_jwt 모드가 pa_worker에 허용되지 않은 RPC를 부르다가 스스로 종료하지 않는다 (게이트에서 발견한 버그)
  * 작업 도중 자격이 폐기되면: 새 선점 중단 → 실행 중 작업 안전 중단 → 종료 요청, 거부된 자격으로 보고를 되풀이하지 않는다
  * 업무 오류(예: 선점하지 않은 Job)는 인증 실패로 오해하지 않는다
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import DbError
from app.main import create_app
from app.storage import SupabaseStorage
from tests.bridge.conftest import TOKEN, make_settings

JWT_MODE = dict(worker_auth="worker_jwt", worker_jwt="wjwt", supabase_anon_key="anon_public", supabase_secret_key="")
REVOKED = DbError(403, "PT403", "FORBIDDEN", "worker credential is missing, expired or revoked")
EXPIRED_TOKEN = DbError(401, "PGRST301", "JWT expired")
NOT_MINE = DbError(403, "PT403", "FORBIDDEN", "job is not claimed by this worker")


class Calls:
    """repo 메서드 호출을 기록하는 도우미."""

    def __init__(self):
        self.names: list[str] = []

    def spy(self, repo, name, result=None, raises=None):
        async def fn(*a, **k):
            self.names.append(name)
            if raises:
                raise raises
            return result
        setattr(repo, name, fn)


def job_dict():
    return {"id": "11111111-1111-4111-8111-111111111111", "locked_at": "2026-01-01T00:00:00+00:00",
            "persona_id": "22222222-2222-4222-8222-222222222222", "content_job_id": "33333333-3333-4333-8333-333333333333"}


# -----------------------------------------------------------------------------
# worker_jwt 모드는 pa_worker가 못 부르는 RPC를 부르지 않는다
# -----------------------------------------------------------------------------
def test_jwt_worker_does_not_sync_the_workflow_registry(env):
    """sync_workflow_registry는 pa_worker의 허용 RPC가 아니다. 부르면 42501이 오고 워커가 스스로 종료했다."""
    env.settings = make_settings(**JWT_MODE)
    calls = Calls()
    calls.spy(env.repo, "sync_workflow_registry", raises=DbError(403, "42501", "permission denied for function"))

    enabled = env.run(env.worker().refresh_nodes())

    assert "sync_workflow_registry" not in calls.names
    assert enabled["image_generation_lora_v1"] is True          # 노드 검사 결과는 그대로 쓴다


def test_secret_key_worker_still_syncs_the_registry(env):
    calls = Calls()
    calls.spy(env.repo, "sync_workflow_registry", result=5)
    env.run(env.worker().refresh_nodes())
    assert calls.names == ["sync_workflow_registry"]


def test_a_status_report_cycle_in_jwt_mode_does_not_stop_the_worker(env):
    env.settings = make_settings(**JWT_MODE, status_report_sec=0.01)
    calls = Calls()
    calls.spy(env.repo, "sync_workflow_registry", raises=DbError(403, "42501", "permission denied"))
    calls.spy(env.repo, "report_worker_status")

    async def go():
        worker = env.worker()
        task = asyncio.create_task(worker.report_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return worker

    worker = env.run(go())
    assert worker.auth_failed is False and worker.stopping is False
    assert calls.names.count("report_worker_status") >= 2


# -----------------------------------------------------------------------------
# 푸시는 local 전용
# -----------------------------------------------------------------------------
def test_push_dispatch_is_disabled_for_jwt_workers(env):
    env.settings = make_settings(**JWT_MODE)
    app = create_app(env.settings, repo=env.repo, storage=env.storage, comfy=env.comfy.client(),
                     notifier=env.notifier, start_background=False)
    with TestClient(app) as client:
        r = client.post("/v1/jobs", headers={"X-Bridge-Token": TOKEN})
    assert r.status_code == 503 and r.json()["error"]["code"] == "PUSH_DISABLED"


# -----------------------------------------------------------------------------
# Storage: 자격 모드별 헤더
# -----------------------------------------------------------------------------
def upload_headers(**kwargs):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async def go():
        storage = SupabaseStorage(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                  "https://ref.supabase.co", **kwargs)
        await storage.upload("media", "persona/p/assets/a.png", b"x", "image/png")

    asyncio.run(go())
    return seen[0].headers


def test_jwt_mode_storage_never_upserts_and_never_sends_a_secret_key():
    h = upload_headers(secret_key="", worker_jwt="wjwt", anon_key="anon_public")
    assert h["x-upsert"] == "false"                           # UPDATE 권한이 없으니 덮어쓰기를 시도하지 않는다
    assert h["authorization"] == "Bearer wjwt" and h["apikey"] == "anon_public"


def test_secret_key_mode_keeps_upsert():
    assert upload_headers(secret_key="sb_secret_abc")["x-upsert"] == "true"


# -----------------------------------------------------------------------------
# 자격이 거부되면: 선점 중단, 실행 중 작업 안전 중단, 종료 요청, 보고를 되풀이하지 않음
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("rejection", [REVOKED, EXPIRED_TOKEN], ids=["revoked", "expired-token"])
def test_heartbeat_rejection_stops_the_running_job_and_requests_exit(env, rejection):
    env.settings = make_settings(**JWT_MODE, heartbeat_sec=0.01)
    calls = Calls()
    calls.spy(env.repo, "heartbeat", raises=rejection)
    calls.spy(env.repo, "fail")
    calls.spy(env.repo, "log_security_event")
    exits = []

    async def go():
        worker = env.worker()
        worker.on_auth_failure = lambda: exits.append(1)
        worker.current = job_dict()
        worker._current_task = asyncio.create_task(asyncio.sleep(60))     # 실행 중인 작업
        worker.pending.append({**job_dict(), "id": "44444444-4444-4444-8444-444444444444"})
        await asyncio.wait_for(worker.heartbeat_loop(), timeout=3)          # 스스로 끝나야 한다
        return worker

    worker = env.run(go())
    assert worker.auth_failed and worker.stopping
    assert worker._current_task.cancelled()                  # 실행 중 작업을 멈췄다
    assert list(worker.pending) == []                        # 대기열도 비웠다
    assert exits == [1]                                      # 종료 요청은 한 번만
    assert calls.names.count("heartbeat") == 1               # 거부된 자격으로 다시 두드리지 않는다
    assert "fail" not in calls.names                         # 거부된 자격으로 실패 보고를 하지 않는다


def test_auth_failure_inside_a_job_stops_the_worker_without_reporting_a_failure(env):
    env.settings = make_settings(**JWT_MODE)
    calls = Calls()
    calls.spy(env.repo, "fail")
    calls.spy(env.repo, "log_security_event")
    exits = []

    async def go():
        worker = env.worker()
        worker.on_auth_failure = lambda: exits.append(1)

        async def rejected(job):
            raise REVOKED
        worker._run = rejected
        await worker.process(job_dict())
        return worker

    worker = env.run(go())
    assert worker.auth_failed and worker.stopping and exits == [1]
    assert "fail" not in calls.names                         # 보고할 수 없다. DB가 Heartbeat 끊김으로 회수한다


def test_shutdown_after_auth_failure_does_not_hammer_supabase(env):
    env.settings = make_settings(**JWT_MODE)
    calls = Calls()
    calls.spy(env.repo, "fail")

    async def go():
        worker = env.worker()
        worker.auth_failed = True
        worker.pending.append(job_dict())
        await worker.shutdown()

    env.run(go())
    assert calls.names == []


def test_shutdown_without_auth_failure_still_returns_jobs_to_retry(env):
    env.settings = make_settings(**JWT_MODE)
    calls = Calls()
    calls.spy(env.repo, "fail", result={"applied": True})

    async def go():
        worker = env.worker()
        worker.pending.append(job_dict())
        await worker.shutdown()

    env.run(go())
    assert calls.names == ["fail"]


def test_a_business_forbidden_is_not_mistaken_for_a_credential_failure(env):
    """'이 워커가 선점한 Job이 아니다'는 자격 문제가 아니다. 워커를 멈추지 않고 그 Job만 실패로 보고한다."""
    env.settings = make_settings(**JWT_MODE)
    calls = Calls()
    calls.spy(env.repo, "fail", result={"applied": False})
    exits = []

    async def go():
        worker = env.worker()
        worker.on_auth_failure = lambda: exits.append(1)

        async def not_mine(job):
            raise NOT_MINE
        worker._run = not_mine
        await worker.process(job_dict())
        return worker

    worker = env.run(go())
    assert worker.auth_failed is False and worker.stopping is False and exits == []
    assert calls.names == ["fail"]


def test_pull_loop_stops_and_requests_exit_when_the_credential_is_revoked(env):
    env.settings = make_settings(**JWT_MODE, pull_jobs=True, pull_interval_sec=0.01)
    calls = Calls()
    calls.spy(env.repo, "claim_next_job", raises=REVOKED)
    exits = []

    async def go():
        worker = env.worker()
        worker.on_auth_failure = lambda: exits.append(1)
        await asyncio.wait_for(worker.pull_loop(), timeout=3)
        return worker

    worker = env.run(go())
    assert worker.auth_failed and exits == [1]
    assert calls.names == ["claim_next_job"]                 # 한 번 거부되면 더 두드리지 않는다
