"""GPU Worker (TECH_DESIGN 13.13, 13.15, 11.6, 14.10).

RTX 5080 하나라서 Worker는 한 번에 한 Job만 실행한다.
선점된 generation Job → Workflow 조립 → 실행 전 검증 → ComfyUI → 실행 후 검증 → 업로드 → Asset 등록 → 완료 보고.

Heartbeat는 **실행 중인 Job과 대기열의 Job 모두**에 보낸다. 대기열의 Job도 DB에서는 이미 processing이라
Heartbeat가 없으면 회수된다 (11.6). 잠금을 잃은 Job은 실행 중이면 멈추고, 대기 중이면 대기열에서 뺀다.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import deque
from typing import Any

import httpx

from app import __version__
from app.comfyui.builder import InputRequest, plan, render, resolve_workflow_id
from app.comfyui.client import ComfyClient, iter_output_files
from app.comfyui.registry import WorkflowSpec, missing_nodes
from app.comfyui.validation import (MIME_EXT, check_models, check_output_names, combo_options, make_thumbnail,
                                    output_invalid, validate_input_image, validate_output)
from app.config import Settings
from app.database import Repository
from app.errors import DbError, JobError, LockLost, transient, validation
from app.security import redact
from app.storage import Storage, safe_path

log = logging.getLogger("bridge.worker")


class Notifier:
    """작업이 최종 완료·실패했을 때 n8n에 알린다 (12.7). 실패해도 재시도하지 않는다 (DB 상태가 이미 맞음)."""

    def __init__(self, client: httpx.AsyncClient | None, url: str, token: str):
        self.client, self.url, self.token = client, url, token

    async def send(self, payload: dict) -> None:
        if not self.url or not self.client:
            return
        try:
            r = await self.client.post(self.url, json=payload, headers={"X-Callback-Token": self.token}, timeout=15)
            r.raise_for_status()
        except Exception as exc:  # 알림 실패로 Worker를 멈추지 않는다
            log.warning("n8n callback failed for job %s: %s", payload.get("job_id"), type(exc).__name__)


class GpuWorker:
    def __init__(self, settings: Settings, repo: Repository, storage: Storage, comfy: ComfyClient,
                 registry: dict[str, WorkflowSpec], notifier: Notifier, rng: random.Random | None = None):
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.comfy = comfy
        self.registry = registry
        self.notifier = notifier
        self.rng = rng or random.Random()
        # 대기열은 Job dict 자체로 관리한다 (같은 id가 다시 선점돼도 locked_at이 다른 별개 항목)
        self.pending: deque[dict] = deque()
        self._wakeup = asyncio.Event()
        self.current: dict | None = None
        self.current_prompt_id: str | None = None
        self.current_step: str | None = None
        self._current_task: asyncio.Task | None = None
        self._stop_reason: str | None = None   # "lock_lost" | "cancelled" | "shutdown"
        self.loop_task: asyncio.Task | None = None
        self.stopping = False
        self.require_loop = True                # 테스트에서 API만 띄울 때 False
        self.disabled: dict[str, str] = {}      # Workflow ID → 꺼진 이유 (노드 미설치)
        self.nodes_checked = False
        self.comfy_ok: bool | None = None

    # -- 대기열 --------------------------------------------------------------------
    @property
    def queued_ids(self) -> list[str]:
        return [j["id"] for j in self.pending]

    def healthy(self) -> bool:
        """루프가 살아 있고 종료 중이 아니면 True. 아니면 API는 Job을 받지 않는다 (503)."""
        if self.stopping:
            return False
        if not self.require_loop:
            return True
        return self.loop_task is not None and not self.loop_task.done()

    def enqueue(self, job: dict) -> int:
        self.pending.append(job)
        self._wakeup.set()
        return len(self.pending) + (1 if self.current else 0)

    async def run_forever(self) -> None:
        while True:
            while not self.pending:
                self._wakeup.clear()
                await self._wakeup.wait()
            job = self.pending.popleft()
            self.current = job
            self._stop_reason = None
            self._current_task = asyncio.create_task(self.process(job))
            try:
                await self._current_task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise  # 루프 자체가 취소됨 (종료)
                # Job만 멈춘 경우 (잠금 상실·취소): 다음 Job으로
            except Exception:  # 어떤 오류도 루프를 죽이지 않는다
                log.exception("job %s: unhandled error in worker loop", job["id"])
            finally:
                self.current, self._current_task, self.current_prompt_id, self.current_step = None, None, None, None

    async def cancel(self, job_id: str) -> bool:
        """12.6 cancel: 대기열이면 빼고, 실행 중이면 ComfyUI 작업을 지우고 멈춘다. DB 상태는 이미 cancelled다."""
        removed = self._drop_queued(lambda j: j["id"] == job_id)
        if self.current and self.current["id"] == job_id and self._current_task:
            await self._stop("cancelled")
            return True
        return bool(removed)

    def _drop_queued(self, predicate) -> list[dict]:
        dropped = [j for j in self.pending if predicate(j)]
        if dropped:
            self.pending = deque(j for j in self.pending if not predicate(j))
        return dropped

    async def _stop(self, reason: str) -> None:
        self._stop_reason = reason
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            await asyncio.gather(self._current_task, return_exceptions=True)

    async def shutdown(self) -> None:
        """종료: 실행 중·대기 중 Job을 재시도 대기로 돌려놓는다 (회수를 기다리지 않게, 리뷰 M2)."""
        self.stopping = True
        jobs = list(self.pending)
        self.pending.clear()
        if self.current and self._current_task:
            jobs.insert(0, self.current)
            await self._stop("shutdown")
        for job in jobs:
            await self._report_failure(job, transient("SHUTDOWN", "bridge shut down before the job finished"))

    # -- Heartbeat (실행 중 + 대기 중) -----------------------------------------------
    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_sec)
            for job in [j for j in (self.current, *self.pending) if j]:
                try:
                    alive = await self.repo.heartbeat(job["id"], job["locked_at"])
                except Exception as exc:  # 일시적 오류: 다음 주기에 다시
                    log.warning("job %s: heartbeat failed (%s)", job["id"], type(exc).__name__)
                    continue
                if alive:
                    continue
                if job is self.current:
                    log.warning("job %s: lock lost while running, stopping", job["id"])
                    await self._stop("lock_lost")
                elif self._drop_queued(lambda j, job=job: j is job):
                    log.warning("job %s: lock lost while queued, dropped", job["id"])

    # -- 실행 ----------------------------------------------------------------------
    async def process(self, job: dict) -> None:
        try:
            await self._run(job)
        except LockLost:
            log.warning("job %s: lock lost, result discarded", job["id"])
        except asyncio.CancelledError:
            log.warning("job %s stopped (%s)", job["id"], self._stop_reason)
            if self.current_prompt_id:  # ComfyUI에 남은 작업을 지운다 (리뷰 M1)
                await asyncio.shield(self.comfy.cancel(self.current_prompt_id))
            raise
        except JobError as exc:
            await self._report_failure(job, exc)
        except Exception as exc:  # 예상하지 못한 오류도 Job은 반드시 실패 처리한다
            log.exception("job %s: unexpected error", job["id"])
            await self._report_failure(job, JobError(f"{type(exc).__name__}: {exc}", error_type="unknown",
                                                     error_code="UNKNOWN", retryable=True))

    async def _log(self, job: dict, step: str, service: str, status: str, **kwargs: Any) -> None:
        if status == "started":
            self.current_step = step
        try:
            await self.repo.log(job["id"], step, service, status, **kwargs)
        except Exception as exc:  # 기록 실패로 작업을 멈추지 않는다
            log.warning("job %s: execution log failed (%s)", job["id"], type(exc).__name__)

    async def _run(self, job: dict) -> None:
        job_id, locked_at, persona_id = job["id"], job["locked_at"], job["persona_id"]
        started = time.monotonic()

        # 0) 이 Job으로 이미 등록된 Asset이 있으면 다시 만들지 않는다 (TECH_DESIGN 49.5 1번).
        #    register_asset 뒤 complete 전에 죽은 Job은 회수·재시도로 다시 선점되는데, 그대로 실행하면 Asset이 또 생긴다.
        existing = await self.repo.get_job_assets(job_id)
        if existing:
            await self._finish_recovered(job, existing, started)
            return

        # 1) 조립 (13.6 ~ 13.8)
        await self._log(job, "BUILD", "python", "started")
        content_job = await self.repo.get_content_job(job["content_job_id"])
        persona = await self.repo.get_persona(persona_id)
        if not content_job or not persona:
            raise validation("INPUT_NOT_FOUND", "content job or persona not found")
        # Persona 격리 (TECH_DESIGN 36.12): 다른 Persona의 Content Job을 가리키는 Job은 ComfyUI를 부르기 전에 실패한다
        if str(content_job["persona_id"]) != str(persona_id):
            raise validation("INPUT_NOT_FOUND", "content job belongs to a different persona")
        persona_assets = await self.repo.get_persona_assets(persona_id)
        workflow_id = resolve_workflow_id(job, content_job, persona)
        spec = self.registry.get(workflow_id or "")
        if not spec or not spec.enabled:
            raise validation("WORKFLOW_INVALID", f"workflow {workflow_id!r} is not registered or disabled")
        if workflow_id in self.disabled:
            raise validation("WORKFLOW_INVALID", f"workflow {workflow_id} unavailable: {self.disabled[workflow_id]}")
        build = plan(spec, job, content_job, persona, persona_assets, self.rng)
        check_models(spec, build.values, await self.comfy.object_info())  # 13.10
        await self._log(job, "BUILD", "python", "succeeded", output=build.metadata)

        # 2) 입력 이미지 (ID → Storage → ComfyUI)
        if build.inputs:
            await self._log(job, "INPUT_UPLOAD", "python", "started")
            for request in build.inputs:
                data, ext = await self._fetch_input(request, persona_id)
                build.values[request.slot] = await self.comfy.upload_image(f"{job_id}_{request.slot}.{ext}", data)
        workflow = render(spec.template, build.values)

        # 3) ComfyUI. prompt_id를 먼저 정해서 보내므로, 요청 도중 취소돼도 지울 수 있다 (리뷰 M1)
        await self._log(job, "COMFYUI_QUEUE", "comfyui", "started")
        self.current_prompt_id = str(uuid.uuid4())
        self.current_prompt_id = await self.comfy.queue_prompt(workflow, self.current_prompt_id)
        wait_started = time.monotonic()
        await self._log(job, "COMFYUI_WAIT", "comfyui", "started", execution_ref=self.current_prompt_id)
        entry = await self.comfy.wait(self.current_prompt_id, self.settings.job_timeout_sec,
                                      self.settings.poll_interval_sec)
        await self._log(job, "COMFYUI_WAIT", "comfyui", "succeeded", execution_ref=self.current_prompt_id,
                        duration_ms=int((time.monotonic() - wait_started) * 1000))

        # 4) 실행 후 검증 (13.11). 이미지 처리는 스레드에서 (Heartbeat가 밀리지 않게)
        await self._log(job, "VALIDATE", "python", "started")
        validate_started = time.monotonic()
        files = list(iter_output_files(entry))
        if not files:
            raise output_invalid("ComfyUI returned no output files")
        try:  # 첫 /view를 부르기 전에 모든 파일 이름을 허용 목록으로 확인한다 (47.3 2번)
            check_output_names(files, job_id, spec)
        except JobError:
            await self._security_event("OUTPUT_UNEXPECTED", {
                "job_id": job_id, "files": [{"filename": str(f.get("filename"))[:200],
                                             "subfolder": str(f.get("subfolder"))[:200]} for f in files[:5]]},
                                       persona_id=persona_id)
            raise
        validated = []
        for f in files:
            data = await self.comfy.view(f["filename"], f.get("subfolder", ""), "output")
            info = await asyncio.to_thread(validate_output, data, spec,
                                           build.values.get("width"), build.values.get("height"))
            thumb = await asyncio.to_thread(make_thumbnail, data)
            validated.append((str(uuid.uuid4()), data, thumb, info))

        await self._log(job, "VALIDATE", "python", "succeeded",
                        duration_ms=int((time.monotonic() - validate_started) * 1000))

        # 5) 전부 업로드한 다음 등록한다 (업로드 실패로 일부만 등록되는 일을 막는다, 리뷰 M4)
        await self._log(job, "UPLOAD", "supabase", "started")
        upload_started = time.monotonic()
        bucket = self.settings.media_bucket
        paths = {}
        for asset_id, data, thumb, info in validated:
            path = f"persona/{persona_id}/assets/{asset_id}.{MIME_EXT[info.mime]}"
            thumb_path = f"persona/{persona_id}/assets/{asset_id}_thumb.webp"
            await self.storage.upload(bucket, path, data, info.mime)
            await self.storage.upload(bucket, thumb_path, thumb, "image/webp")
            paths[asset_id] = (path, thumb_path)
        asset_ids: list[str] = []
        for index, (asset_id, _data, _thumb, info) in enumerate(validated):
            path, thumb_path = paths[asset_id]
            row = await self.repo.register_asset(job_id, locked_at, {
                "id": asset_id, "asset_type": spec.type, "file_name": path.rsplit("/", 1)[1],
                "storage_bucket": bucket, "storage_path": path,
                "public_url": self.storage.public_url(bucket, path),
                "thumbnail_url": self.storage.public_url(bucket, thumb_path),
                "mime_type": info.mime, "width": info.width, "height": info.height,
                "prompt": build.values.get("prompt"), "workflow": workflow,
                "generation_metadata": {**build.metadata, "batch_index": index, "comfy_prompt_id": self.current_prompt_id},
            })
            if row is None:
                raise LockLost()
            asset_ids.append(str(row["id"]))
        await self._log(job, "UPLOAD", "supabase", "succeeded",
                        duration_ms=int((time.monotonic() - upload_started) * 1000))

        # 6) 완료
        result = {"asset_ids": asset_ids, "comfy_prompt_id": self.current_prompt_id,
                  "oom_downscaled": build.oom_downscaled}
        if not await self.repo.complete(job_id, locked_at, result):
            raise LockLost()
        await self._log(job, "COMPLETE", "python", "succeeded",
                        duration_ms=int((time.monotonic() - started) * 1000), output={"asset_ids": asset_ids})
        log.info("job %s done: %d asset(s)", job_id, len(asset_ids))
        await self.notifier.send(self._event(job, "generation.completed", "done", asset_ids, None))

    async def _finish_recovered(self, job: dict, assets: list[dict], started: float) -> None:
        """이미 등록된 Asset으로 Job을 끝낸다. 생성하지 않는다 (11.4: Asset 1개 이상이면 generation 완료)."""
        job_id, locked_at = job["id"], job["locked_at"]
        asset_ids = [str(a["id"]) for a in assets]
        if not await self.repo.complete(job_id, locked_at, {"asset_ids": asset_ids, "recovered": True}):
            raise LockLost()
        await self._log(job, "RECOVERED", "python", "succeeded", output={"asset_ids": asset_ids})
        await self._log(job, "COMPLETE", "python", "succeeded",
                        duration_ms=int((time.monotonic() - started) * 1000), output={"asset_ids": asset_ids})
        log.info("job %s recovered: %d existing asset(s), nothing generated", job_id, len(asset_ids))
        await self.notifier.send(self._event(job, "generation.completed", "done", asset_ids, None))

    async def _security_event(self, event_type: str, detail: dict, persona_id: str | None = None) -> None:
        try:  # persona_id가 있어야 Operator가 자기 화면에서 볼 수 있다 (security_events_select_own)
            await self.repo.log_security_event(event_type, None, detail, actor_type="python", persona_id=persona_id)
        except Exception as exc:  # 기록 실패로 오류 보고를 막지 않는다
            log.warning("security event %s not recorded (%s)", event_type, type(exc).__name__)

    async def _fetch_input(self, request: InputRequest, persona_id: str) -> tuple[bytes, str]:
        if request.kind == "asset":
            row = await self.repo.get_asset(request.id)
            if not row or str(row["persona_id"]) != str(persona_id) or row["status"] == "archived":
                raise validation("INPUT_NOT_FOUND", f"asset {request.id} not found for persona")
            bucket, path = row["storage_bucket"], row["storage_path"]
        else:
            row = request.row or {}
            bucket, path = self.settings.private_bucket, row.get("storage_path", "")
            if not path.startswith(f"persona/{persona_id}/refs/"):
                raise validation("INPUT_NOT_FOUND", f"persona asset {request.id} has an invalid path")
        try:
            safe_path(path)
        except ValueError as exc:
            raise validation("INPUT_NOT_FOUND", str(exc)) from exc
        data = await self.storage.download(bucket, path)
        return data, await asyncio.to_thread(validate_input_image, data)

    async def _report_failure(self, job: dict, exc: JobError) -> None:
        """fail_automation_job 보고. 여기서 어떤 예외도 밖으로 내보내지 않는다 (리뷰 H2)."""
        step = exc.step or self.current_step or "UNKNOWN"
        try:
            message = redact(str(exc), (self.settings.supabase_secret_key,))
            log.warning("job %s failed at %s: %s %s", job["id"], step, exc.error_code, message)
            await self._log(job, step, "python", "failed", error=message)
            result = await self.repo.fail(job["id"], job["locked_at"], exc.error_type, exc.error_code, message,
                                          exc.retryable, step=step)
            if isinstance(result, dict) and result.get("applied") and result.get("status") == "failed":
                await self.notifier.send(self._event(job, "generation.failed", "failed", [],
                                                     {"type": exc.error_type, "code": exc.error_code,
                                                      "message": message}))
        except Exception:
            log.exception("job %s: could not report failure (heartbeat recovery will retry it)", job["id"])

    @staticmethod
    def _event(job: dict, event: str, status: str, asset_ids: list[str], error: dict | None) -> dict:
        return {"event": event, "job_id": job["id"], "content_job_id": job.get("content_job_id"),
                "persona_id": job["persona_id"], "status": status, "attempts": job.get("attempts"),
                "asset_ids": asset_ids, "error": error}

    # -- 상태 보고 (17.4) ------------------------------------------------------------
    async def refresh_nodes(self) -> dict[str, bool]:
        """설치되지 않은 노드를 쓰는 Workflow는 끈다. 결과를 comfy_workflows에 동기화한다."""
        self.comfy.invalidate_object_info()
        info = await self.comfy.object_info()
        self.disabled = {}
        for spec in self.registry.values():
            missing = missing_nodes(spec, info)
            if missing:
                self.disabled[spec.id] = f"missing ComfyUI nodes: {sorted(missing)}"
        self.nodes_checked = True
        enabled = {wid: spec.enabled and wid not in self.disabled for wid, spec in self.registry.items()}
        await self.repo.sync_workflow_registry({wid: spec.sync_payload(enabled[wid]) for wid, spec in self.registry.items()})
        return enabled

    async def installed_models(self) -> dict | None:
        """ComfyUI에 설치된 체크포인트·LoRA 이름 (22.9 Visual Identity 드롭다운). 읽지 못하면 None."""
        try:
            info = await self.comfy.object_info()  # 5분 캐시라 새 파일은 최대 5분 늦게 보인다
        except Exception as exc:  # 목록을 못 읽어도 상태 보고는 계속한다 (DB는 이전 목록 유지)
            log.warning("could not read installed models: %s", type(exc).__name__)
            return None
        return {"checkpoints": sorted(combo_options(info, "CheckpointLoaderSimple", "ckpt_name") or []),
                "loras": sorted(combo_options(info, "LoraLoader", "lora_name") or [])}

    async def status_info(self) -> dict:
        gpu: dict = {}
        models = None
        try:
            stats = await self.comfy.system_stats()
            self.comfy_ok = True
            device = (stats.get("devices") or [{}])[0]
            gpu = {"name": device.get("name"),
                   "vram_total_mb": round(device.get("vram_total", 0) / 2**20),
                   "vram_free_mb": round(device.get("vram_free", 0) / 2**20)}
            models = await self.installed_models()
        except Exception:
            self.comfy_ok = False
        info = {"comfyui_ok": self.comfy_ok, "gpu": gpu,
                "current_job_id": self.current["id"] if self.current else None,
                "queue_size": len(self.pending), "version": __version__, "worker_loop_ok": self.healthy()}
        if models is not None:  # 없으면 DB의 이전 목록을 유지한다 (0008)
            info["models"] = models
        return info

    async def report_loop(self) -> None:
        while True:
            try:
                info = await self.status_info()
                if info["comfyui_ok"] and not self.nodes_checked:
                    await self.refresh_nodes()
                await self.repo.report_worker_status(self.settings.worker_id, info)
            except (DbError, httpx.HTTPError) as exc:  # Supabase에 잠시 연결할 수 없음
                log.warning("status report failed: %s", type(exc).__name__)
            except Exception:  # 상태 보고 실패로 Worker를 멈추지 않는다
                log.exception("status report failed")
            await asyncio.sleep(self.settings.status_report_sec)
