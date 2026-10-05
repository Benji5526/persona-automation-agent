"""로컬 ComfyUI HTTP API 클라이언트 (TECH_DESIGN 13.15).

ComfyUI는 127.0.0.1에서만 열려 있다 (15.7). 이 클라이언트만 ComfyUI에 접근한다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx

from app.errors import JobError, transient, validation


def _unreachable(exc: Exception) -> JobError:
    return transient("COMFY_UNREACHABLE", f"ComfyUI unreachable: {type(exc).__name__}: {exc}")


def classify_execution_error(node_type: str | None, node_id: str | None, message: str) -> JobError:
    """ComfyUI 실행 오류 → error_code (13.12)."""
    text = f"{node_type} (node {node_id}): {message.strip()}"
    lower = message.lower()
    if "out of memory" in lower or "outofmemory" in lower:
        return JobError(text, error_type="generation", error_code="OUT_OF_MEMORY", retryable=True)
    if "cuda" in lower:
        return JobError(text, error_type="generation", error_code="CUDA_ERROR", retryable=True)
    return JobError(text, error_type="generation", error_code="NODE_ERROR", retryable=False)


class ComfyClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str, object_info_cache_sec: float = 300.0):
        self.client = client
        self.base = base_url.rstrip("/")
        self.client_id = uuid.uuid4().hex
        self._object_info: tuple[float, dict] | None = None
        self._object_info_ttl = object_info_cache_sec

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.client.get(f"{self.base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc

    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.client.post(f"{self.base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc

    async def system_stats(self) -> dict:
        r = await self._get("/system_stats", timeout=3)
        r.raise_for_status()
        return r.json()

    async def object_info(self) -> dict:
        """설치된 노드와 각 입력의 선택지 (모델·LoRA 파일 목록 포함). 5분 캐시."""
        now = time.monotonic()
        if self._object_info and now - self._object_info[0] < self._object_info_ttl:
            return self._object_info[1]
        r = await self._get("/object_info", timeout=30)
        if r.status_code != 200:
            raise transient("COMFY_UNREACHABLE", f"object_info failed ({r.status_code})")
        info = r.json()
        self._object_info = (now, info)
        return info

    def invalidate_object_info(self) -> None:
        self._object_info = None

    async def upload_image(self, filename: str, data: bytes) -> str:
        r = await self._post("/upload/image", files={"image": (filename, data)}, data={"overwrite": "true"})
        if r.status_code != 200:
            raise transient("FILE_ERROR", f"ComfyUI input upload failed ({r.status_code}): {r.text[:300]}")
        info = r.json()
        return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]

    async def queue_prompt(self, workflow: dict, prompt_id: str | None = None) -> str:
        """prompt_id를 미리 정해서 보낸다 (최근 ComfyUI는 이 값을 그대로 쓴다). 응답의 값을 최종으로 쓴다."""
        body: dict[str, Any] = {"prompt": workflow, "client_id": self.client_id}
        if prompt_id:
            body["prompt_id"] = prompt_id
        r = await self._post("/prompt", json=body)
        if r.status_code == 400:
            # node_errors에 어떤 노드가 왜 틀렸는지가 들어 있다 (모델 이름 오타, 없는 노드 등)
            raise validation("WORKFLOW_INVALID", f"ComfyUI rejected the workflow: {r.text[:2000]}")
        if r.status_code != 200:
            raise transient("COMFY_UNREACHABLE", f"ComfyUI /prompt failed ({r.status_code}): {r.text[:300]}")
        return r.json()["prompt_id"]

    async def wait(self, prompt_id: str, timeout_sec: float, poll_sec: float,
                   on_poll: Callable[[], Awaitable[None]] | None = None) -> dict:
        """/history에 결과가 나올 때까지 기다린다. 시간을 넘기면 대기열에서 지우고 실행을 중단한다."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            r = await self._get(f"/history/{prompt_id}")
            if r.status_code == 200:
                entry = r.json().get(prompt_id)
                if entry:
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        raise self._execution_error(entry)
                    return entry
            if on_poll:
                await on_poll()
            await asyncio.sleep(poll_sec)
        await self.cancel(prompt_id)
        raise JobError(f"ComfyUI timed out after {timeout_sec:.0f}s", error_type="timeout",
                       error_code="TIMEOUT", retryable=True)

    @staticmethod
    def _execution_error(entry: dict) -> JobError:
        for msg in entry.get("status", {}).get("messages", []):
            if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
                d = msg[1]
                return classify_execution_error(d.get("node_type"), d.get("node_id"), d.get("exception_message", ""))
            if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_interrupted":
                # 다른 곳에서 중단됨 (ComfyUI 화면에서 취소 등). 다시 시도하면 될 수 있다
                return transient("INTERRUPTED", "ComfyUI execution was interrupted")
        return JobError("ComfyUI execution error (no details)", error_type="generation",
                        error_code="NODE_ERROR", retryable=False)

    async def cancel(self, prompt_id: str) -> None:
        """이 prompt만 정리한다: 대기열에서 지우고, **지금 실행 중인 것이 이 prompt일 때만** 중단한다.
        /interrupt는 실행 중인 작업 전체에 걸리므로 다른 작업을 멈추지 않게 먼저 확인한다. 실패는 무시한다."""
        try:
            await self.client.post(f"{self.base}/queue", json={"delete": [prompt_id]})
            r = await self.client.get(f"{self.base}/queue")
            running = r.json().get("queue_running", []) if r.status_code == 200 else []
            if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in running):
                await self.client.post(f"{self.base}/interrupt")
        except (httpx.HTTPError, ValueError):
            pass

    async def view(self, filename: str, subfolder: str, file_type: str) -> bytes:
        r = await self._get("/view", params={"filename": filename, "subfolder": subfolder, "type": file_type})
        if r.status_code != 200:
            raise transient("FILE_ERROR", f"ComfyUI /view failed ({r.status_code}) for {filename}")
        return r.content


def iter_output_files(entry: dict) -> Iterator[dict]:
    """최종 출력 파일만 (type = output). PreviewImage 같은 temp는 건너뛴다.

    filename·subfolder는 ComfyUI /history 응답에서 받은 값만 쓴다 (15.17).
    """
    for node_output in entry.get("outputs", {}).values():
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item and item.get("type") == "output":
                    yield item
