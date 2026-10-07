"""환경변수 설정 (TECH_DESIGN 16.5)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
MIN_TOKEN_LENGTH = 32  # 15.6: 32바이트 이상 무작위 값


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_secret_key: str
    bridge_tokens: tuple[str, ...]
    worker_id: str = "python:rtx5080-1"
    comfy_url: str = "http://127.0.0.1:8188"
    workflow_dir: Path = ROOT / "workflows"
    work_dir: Path = ROOT / "data" / "tmp"
    media_bucket: str = "media"
    private_bucket: str = "persona-private"
    job_timeout_sec: float = 900.0
    poll_interval_sec: float = 1.0
    pull_jobs: bool = False            # True면 놀고 있을 때 DB에서 generation Job을 가져간다 (TECH_DESIGN 56.3)
    pull_interval_sec: float = 5.0
    heartbeat_sec: float = 30.0
    status_report_sec: float = 30.0
    comfy_check_cache_sec: float = 5.0
    object_info_cache_sec: float = 300.0
    n8n_callback_url: str = ""
    n8n_callback_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    jobs_rate_per_sec: float = 5.0
    auth_fail_limit: int = 10          # 1분에 이만큼 토큰 오류면
    auth_block_sec: float = 600.0      # 10분 차단 (15.8)
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        supabase = urlparse(self.supabase_url)
        if supabase.scheme != "https" and supabase.hostname not in ("127.0.0.1", "localhost"):
            raise ConfigError("SUPABASE_URL must be an https URL")
        if not self.supabase_secret_key:
            raise ConfigError("SUPABASE_SECRET_KEY is required")
        if not self.bridge_tokens:
            raise ConfigError("BRIDGE_TOKENS is required")
        short = [t for t in self.bridge_tokens if len(t) < MIN_TOKEN_LENGTH]
        if short:
            raise ConfigError(f"each bridge token must be at least {MIN_TOKEN_LENGTH} characters")
        comfy = urlparse(self.comfy_url)
        if comfy.scheme != "http" or comfy.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ConfigError("COMFY_URL must point to localhost (ComfyUI must not be exposed, 15.7)")

    @classmethod
    def from_env(cls) -> "Settings":
        def get(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        tokens = tuple(t.strip() for t in get("BRIDGE_TOKENS").split(",") if t.strip())
        return cls(
            supabase_url=get("SUPABASE_URL").rstrip("/"),
            supabase_secret_key=get("SUPABASE_SECRET_KEY") or get("SUPABASE_SERVICE_ROLE_KEY"),
            bridge_tokens=tokens,
            worker_id=get("WORKER_ID", "python:rtx5080-1"),
            comfy_url=get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/"),
            workflow_dir=Path(get("WORKFLOW_DIR") or ROOT / "workflows"),
            work_dir=Path(get("WORK_DIR") or ROOT / "data" / "tmp"),
            job_timeout_sec=float(get("JOB_TIMEOUT_SEC", "900")),
            poll_interval_sec=float(get("POLL_INTERVAL_SEC", "1.0")),
            pull_jobs=get("PULL_JOBS", "false").lower() in ("1", "true", "yes", "on"),
            pull_interval_sec=float(get("PULL_INTERVAL_SEC", "5")),
            heartbeat_sec=float(get("HEARTBEAT_SEC", "30")),
            status_report_sec=float(get("STATUS_REPORT_SEC", "30")),
            n8n_callback_url=get("N8N_CALLBACK_URL"),
            n8n_callback_token=get("N8N_CALLBACK_TOKEN"),
            host=get("BRIDGE_HOST", "127.0.0.1"),
            port=int(get("BRIDGE_PORT", "8000")),
            log_level=get("LOG_LEVEL", "INFO"),
        )
