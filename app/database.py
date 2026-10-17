"""Supabase 접근 계층 (TECH_DESIGN 12.5 Worker API).

Repository 인터페이스 뒤에 숨긴다.
* PostgrestRepository: 실제 실행. PostgREST(/rest/v1)를 httpx로 직접 호출한다.
* 테스트는 같은 인터페이스를 psycopg로 구현해 실제 마이그레이션 위에서 돌린다 (tests/bridge).

locked_at은 DB가 돌려준 문자열을 그대로 다시 보낸다 (잠금 확인은 정확히 같은 값이어야 한다).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.config import auth_headers
from app.errors import DbError

Row = dict[str, Any]


class Repository(Protocol):
    async def claim_job(self, job_id: str, worker: str) -> Row | None: ...
    async def claim_next_job(self, job_type: str, worker: str) -> Row | None: ...
    async def get_job(self, job_id: str) -> Row | None: ...
    async def get_job_context(self, job_id: str) -> Row: ...
    async def get_input_asset(self, job_id: str, asset_id: str) -> Row | None: ...
    async def heartbeat(self, job_id: str, locked_at: str) -> bool: ...
    async def complete(self, job_id: str, locked_at: str, result: dict) -> bool: ...
    async def fail(self, job_id: str, locked_at: str, error_type: str, error_code: str, message: str,
                   retryable: bool, step: str | None = None, retry_after_seconds: int | None = None) -> dict: ...
    async def log(self, job_id: str, step: str, service: str, status: str, *, input: dict | None = None,
                  output: dict | None = None, duration_ms: int | None = None, execution_ref: str | None = None,
                  error: str | None = None) -> None: ...
    async def register_asset(self, job_id: str, locked_at: str, asset: dict) -> Row | None: ...
    async def sync_workflow_registry(self, workflows: dict) -> int: ...
    async def report_worker_status(self, worker_id: str, info: dict) -> None: ...
    async def log_security_event(self, event_type: str, source_ip: str | None, detail: dict,
                                 actor_type: str = "anonymous", persona_id: str | None = None) -> None: ...


def _first(value: Any) -> Row | None:
    """setof RPC는 배열, 복합 타입 RPC는 객체를 돌려준다."""
    if isinstance(value, list):
        return value[0] if value else None
    if isinstance(value, dict) and value.get("id") is not None:
        return value
    return None


class PostgrestRepository:
    def __init__(self, client: httpx.AsyncClient, supabase_url: str, secret_key: str, actor: str = "python",
                 *, worker_jwt: str = "", anon_key: str = ""):
        self.client = client
        self.base = f"{supabase_url.rstrip('/')}/rest/v1"
        self.headers = {
            "Content-Type": "application/json", "x-actor": actor,
            **auth_headers(secret_key=secret_key, worker_jwt=worker_jwt, anon_key=anon_key),
        }

    async def _rpc(self, fn: str, params: dict) -> Any:
        r = await self.client.post(f"{self.base}/rpc/{fn}", headers=self.headers, content=json.dumps(params))
        return self._parse(r)

    async def _select(self, table: str, query: dict[str, str]) -> list[Row]:
        r = await self.client.get(f"{self.base}/{table}", headers=self.headers, params={"select": "*", **query})
        return self._parse(r)

    @staticmethod
    def _parse(r: httpx.Response) -> Any:
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"message": r.text[:500]}
            raise DbError(r.status_code, body.get("code"), body.get("message", ""), body.get("details"))
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # -- Job -------------------------------------------------------------------
    async def claim_job(self, job_id: str, worker: str) -> Row | None:
        return _first(await self._rpc("claim_automation_job", {"p_job_id": job_id, "p_worker": worker}))

    async def claim_next_job(self, job_type: str, worker: str) -> Row | None:
        return _first(await self._rpc("claim_next_automation_job", {"p_job_type": job_type, "p_worker": worker}))

    async def get_job(self, job_id: str) -> Row | None:
        rows = await self._select("automation_jobs", {"id": f"eq.{job_id}"})
        return rows[0] if rows else None

    async def heartbeat(self, job_id: str, locked_at: str) -> bool:
        return bool(await self._rpc("heartbeat_automation_job", {"p_job_id": job_id, "p_locked_at": locked_at}))

    async def complete(self, job_id: str, locked_at: str, result: dict) -> bool:
        return bool(await self._rpc("complete_automation_job",
                                    {"p_job_id": job_id, "p_locked_at": locked_at, "p_result": result}))

    async def fail(self, job_id, locked_at, error_type, error_code, message, retryable, step=None,
                   retry_after_seconds=None) -> dict:
        return await self._rpc("fail_automation_job", {
            "p_job_id": job_id, "p_locked_at": locked_at, "p_error_type": error_type, "p_error_code": error_code,
            "p_message": message, "p_retryable": retryable, "p_retry_after_seconds": retry_after_seconds,
            "p_step": step})

    async def log(self, job_id, step, service, status, *, input=None, output=None, duration_ms=None,
                  execution_ref=None, error=None) -> None:
        await self._rpc("log_execution", {
            "p_job_id": job_id, "p_step": step, "p_service": service, "p_status": status, "p_input": input,
            "p_output": output, "p_duration_ms": duration_ms, "p_execution_ref": execution_ref, "p_error": error})

    async def register_asset(self, job_id: str, locked_at: str, asset: dict) -> Row | None:
        return _first(await self._rpc("register_asset", {"p_job_id": job_id, "p_locked_at": locked_at, "p_asset": asset}))

    # -- 실행 맥락 ----------------------------------------------------------------
    # 직접 SELECT를 쓰지 않는다 (TECH_DESIGN 56.5): pa_worker는 테이블 권한이 없고,
    # 이 RPC는 자기가 선점한 Job에 필요한 것만 돌려준다. SNS 토큰·다른 Persona는 포함되지 않는다.
    async def get_job_context(self, job_id: str) -> Row:
        return await self._rpc("worker_get_job_context", {"p_job_id": job_id}) or {}

    async def get_input_asset(self, job_id: str, asset_id: str) -> Row | None:
        return await self._rpc("worker_get_input_asset", {"p_job_id": job_id, "p_asset_id": asset_id}) or None

    # -- 운영 --------------------------------------------------------------------
    async def sync_workflow_registry(self, workflows: dict) -> int:
        return int(await self._rpc("sync_workflow_registry", {"p_workflows": workflows}) or 0)

    async def report_worker_status(self, worker_id: str, info: dict) -> None:
        await self._rpc("report_worker_status", {"p_worker_id": worker_id, "p_kind": "python", "p_info": info})

    async def log_security_event(self, event_type: str, source_ip: str | None, detail: dict,
                                 actor_type: str = "anonymous", persona_id: str | None = None) -> None:
        await self._rpc("log_security_event", {"p_event_type": event_type, "p_actor_type": actor_type,
                                               "p_source_ip": source_ip, "p_detail": detail,
                                               "p_persona_id": persona_id})
