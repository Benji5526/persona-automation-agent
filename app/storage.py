"""Supabase Storage 접근 (TECH_DESIGN 15.5).

* media (공개): 생성 결과물 persona/{persona_id}/assets/{asset_id}.{ext}
* persona-private (비공개): 참조 이미지 persona/{persona_id}/refs/…
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import auth_headers
from app.errors import transient, validation


class Storage(Protocol):
    async def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None: ...
    async def download(self, bucket: str, path: str) -> bytes: ...
    def public_url(self, bucket: str, path: str) -> str: ...


def _not_found_body(r: httpx.Response) -> bool:
    """Supabase Storage는 없는 파일에 400 + {"statusCode": "404"}를 돌려주기도 한다."""
    try:
        body = r.json()
    except ValueError:
        return False
    return str(body.get("statusCode")) == "404" or str(body.get("error", "")).lower() in ("not_found", "not found")


def safe_path(path: str) -> str:
    """경로 조작 방지 (15.17): 상대 경로·빈 구간·백슬래시를 허용하지 않는다."""
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe storage path: {path!r}")
    return path


class SupabaseStorage:
    def __init__(self, client: httpx.AsyncClient, supabase_url: str, secret_key: str,
                 *, worker_jwt: str = "", anon_key: str = ""):
        self.client = client
        self.base = f"{supabase_url.rstrip('/')}/storage/v1"
        # worker_jwt면 service_role 키를 쓰지 않는다 (56.5).
        self.headers = auth_headers(secret_key=secret_key, worker_jwt=worker_jwt, anon_key=anon_key)
        # pa_worker에는 storage.objects의 UPDATE 권한이 없다 → 덮어쓰기(upsert)는 정책상 불가능하고 시도해서도 안 된다.
        # 결과 파일 이름(asset id)은 시도마다 새로 만들어지므로 upsert가 필요하지 않다 (0011 8절).
        self.upsert = "false" if worker_jwt else "true"

    def _object_url(self, bucket: str, path: str) -> str:
        return f"{self.base}/object/{quote(bucket)}/{quote(safe_path(path))}"

    async def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        r = await self.client.post(self._object_url(bucket, path), content=data,
                                   headers={**self.headers, "Content-Type": content_type, "x-upsert": self.upsert})
        if r.status_code >= 400:
            raise transient("FILE_ERROR", f"storage upload failed ({r.status_code}): {r.text[:300]}")

    async def download(self, bucket: str, path: str) -> bytes:
        r = await self.client.get(self._object_url(bucket, path), headers=self.headers)
        if r.status_code == 404 or (r.status_code == 400 and _not_found_body(r)):
            raise validation("INPUT_NOT_FOUND", f"storage object not found: {bucket}/{path}")
        if r.status_code >= 400:
            raise transient("FILE_ERROR", f"storage download failed ({r.status_code})")
        return r.content

    def public_url(self, bucket: str, path: str) -> str:
        return f"{self.base}/object/public/{quote(bucket)}/{quote(safe_path(path))}"
