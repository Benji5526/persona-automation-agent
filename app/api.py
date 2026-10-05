"""Bridge API /v1 (TECH_DESIGN 12.6)."""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import DbError
from app.security import token_matches

log = logging.getLogger("bridge.api")
router = APIRouter(prefix="/v1")
MAX_BODY_BYTES = 4096


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}},
                        headers={"X-Content-Type-Options": "nosniff"})


def client_ip(request: Request) -> str:
    # Cloudflare Tunnel 뒤에서는 원래 IP가 이 헤더에 온다
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def ip_or_none(value: str) -> str | None:
    """DB inet 칸에는 실제 IP만 넣는다 (형식이 아니면 비움)."""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


async def authorize(request: Request) -> JSONResponse | None:
    """토큰 확인 + 반복 실패 IP 차단 (15.8). 통과하면 None."""
    state = request.app.state
    ip = client_ip(request)
    if state.auth_tracker.is_blocked(ip):
        return error(429, "BLOCKED", "too many failed attempts")
    if token_matches(request.headers.get("x-bridge-token"), state.settings.bridge_tokens):
        return None
    blocked_now = state.auth_tracker.record_failure(ip)
    # 인증 없는 요청마다 DB에 쓰지 않는다: IP별 첫 실패와 차단될 때만 기록 (리뷰 L4)
    if blocked_now or ip not in state.auth_logged:
        state.auth_logged.add(ip)
        if len(state.auth_logged) > 10_000:
            state.auth_logged.clear()
        try:
            await state.repo.log_security_event("API_AUTH_FAILED", ip_or_none(ip),
                                                {"path": request.url.path, "blocked": blocked_now, "client": ip[:64]})
        except Exception:
            log.warning("could not record API_AUTH_FAILED")
    return error(401, "INVALID_TOKEN", "invalid bridge token")


async def comfy_available(request: Request) -> bool:
    """선점 전에 ComfyUI 연결 확인 (5초 캐시). 꺼져 있으면 선점하지 않아 attempts가 늘지 않는다."""
    state = request.app.state
    now = time.monotonic()
    cached = state.comfy_check
    if cached and now - cached[0] < state.settings.comfy_check_cache_sec:
        return cached[1]
    try:
        await state.comfy.system_stats()
        ok = True
    except Exception:
        ok = False
    state.comfy_check = (now, ok)
    return ok


async def read_limited(request: Request) -> bytes | None:
    """본문을 최대 MAX_BODY_BYTES까지만 읽는다 (chunked 전송도). 넘으면 None."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/jobs")
async def create_job(request: Request):
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
        return error(413, "INVALID_REQUEST", "body too large or invalid Content-Length")
    denied = await authorize(request)
    if denied:
        return denied
    state = request.app.state
    if not state.rate_limiter.allow():
        return error(429, "RATE_LIMITED", "too many requests")
    if not state.worker.healthy():
        return error(503, "WORKER_UNAVAILABLE", "GPU worker is not running")

    raw = await read_limited(request)
    if raw is None:
        return error(413, "INVALID_REQUEST", "body too large")
    job_id = None
    if raw.strip():
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) - {"job_id"}:
                raise ValueError("only job_id is allowed")
            if body.get("job_id") is not None:
                job_id = str(uuid.UUID(str(body["job_id"])))
        except (ValueError, TypeError) as exc:
            return error(422, "INVALID_REQUEST", str(exc))

    if not await comfy_available(request):
        return error(503, "COMFY_UNAVAILABLE", "ComfyUI is not reachable")

    worker_id = state.settings.worker_id
    try:
        if job_id:
            existing = await state.repo.get_job(job_id)
            if existing and existing["job_type"] != "generation":
                return error(422, "INVALID_REQUEST", "only generation jobs can be sent to the bridge")
            claimed = await state.repo.claim_job(job_id, worker_id) if existing else None
        else:
            claimed = await state.repo.claim_next_job("generation", worker_id)
    except DbError as exc:
        log.error("claim failed: %s", exc)
        return error(502, "DATABASE_ERROR", "could not claim job")
    if not claimed:
        return error(409, "JOB_NOT_CLAIMABLE", "no claimable pending job")

    position = state.worker.enqueue(claimed)
    return JSONResponse(status_code=202, content={"accepted": True, "job_id": claimed["id"], "queue_position": position},
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    return {"ok": state.worker.healthy(), "comfyui": await comfy_available(request),
            "queue_size": len(state.worker.pending), "busy": state.worker.current is not None}


@router.get("/status")
async def status(request: Request):
    denied = await authorize(request)
    if denied:
        return denied
    worker = request.app.state.worker
    info = await worker.status_info()
    return {**info, "worker_id": request.app.state.settings.worker_id,
            "queued_job_ids": list(worker.queued_ids),
            "workflows": {wid: {"version": s.version, "enabled": s.enabled and wid not in worker.disabled,
                                "disabled_reason": worker.disabled.get(wid)}
                          for wid, s in worker.registry.items()}}


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str, request: Request):
    denied = await authorize(request)
    if denied:
        return denied
    if await request.app.state.worker.cancel(job_id):
        return {"cancelled": True}
    return error(404, "NOT_FOUND", "job is not queued or running on this bridge")
