"""Worker JWT 발급 (TECH_DESIGN 56.5).

**소유자 PC에서만 실행한다.** 서명 키(`SUPABASE_JWT_SECRET`)는 이 PC에만 두고,
Cloud GPU Pod에는 **절대 넣지 않는다**. Pod에 들어가는 것은 발급된 Worker JWT와 공개 anon 키뿐이다.

하는 일
  1. jti를 만들고 `issue_worker_credential`로 대장에 등록한다 (worker_id·target·provider·만료)
  2. 그 jti로 Worker JWT를 서명한다 (role=pa_worker, 수명 24시간 이하)
  3. Pod `.env`에 붙여 넣을 줄을 출력한다

사용 예
  python scripts/mint_worker_token.py --worker-id python:cloud-1 --target cloud --provider runpod --hours 8

필요한 환경변수
  SUPABASE_URL          https://<project-ref>.supabase.co
  SUPABASE_JWT_SECRET   JWT 서명 키 (Project Settings → API → JWT Secret). 이 PC에만 둔다
  SUPABASE_ANON_KEY     공개 anon 키 (PostgREST의 apikey 헤더용)
  PA_OWNER_USER_ID      소유자(users.role = 'admin')의 users.id

자격을 폐기할 때는 Lovable의 Worker Credentials 화면이나 `revoke_worker_credential(jti)`를 쓴다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

MAX_HOURS = 24          # 사양 16조: Worker JWT 수명은 24시간 이하
OWNER_TOKEN_SECONDS = 300   # 자격 등록에만 쓰는 짧은 소유자 토콘
TARGETS = ("local", "cloud")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt(claims: dict, secret: str) -> str:
    """HS256 JWT. 외부 의존성을 늘리지 않으려고 표준 라이브러리로만 만든다."""
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def worker_claims(worker_id: str, target: str, provider: str | None, jti: uuid.UUID,
                  issued_at: int, expires_at: int) -> dict:
    """Worker JWT의 클레임. private.worker_identity()가 확인하는 이름과 정확히 맞아야 한다 (56.5)."""
    return {"role": "pa_worker", "worker_id": worker_id, "target": target,
            "provider": provider or target, "jti": str(jti),
            "iat": issued_at, "exp": expires_at}


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"error: {name} is required (see the docstring)")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a Worker JWT for a persona-automation-agent worker")
    parser.add_argument("--worker-id", required=True, help="e.g. python:cloud-1 (no GPU model in the name)")
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--provider", default=None, help="e.g. local, runpod")
    parser.add_argument("--hours", type=float, default=8.0, help=f"token lifetime, at most {MAX_HOURS}")
    args = parser.parse_args()

    if not 0 < args.hours <= MAX_HOURS:
        sys.exit(f"error: --hours must be between 0 and {MAX_HOURS}")

    supabase_url = env("SUPABASE_URL").rstrip("/")
    jwt_secret = env("SUPABASE_JWT_SECRET")
    anon_key = env("SUPABASE_ANON_KEY")
    owner_id = env("PA_OWNER_USER_ID")

    jti = uuid.uuid4()
    now = int(time.time())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=args.hours)

    # 1) 대장에 등록한다. issue_worker_credential은 admin만 부를 수 있으므로(require_admin)
    #    서명 키로 짧은 소유자 토큰을 만들어 쓴다. 이 토큰은 네트워크로만 나가고 저장하지 않는다.
    owner_token = sign_jwt(
        {"sub": owner_id, "role": "authenticated", "iat": now, "exp": now + OWNER_TOKEN_SECONDS},
        jwt_secret)
    response = httpx.post(
        f"{supabase_url}/rest/v1/rpc/issue_worker_credential",
        headers={"apikey": anon_key, "Authorization": f"Bearer {owner_token}",
                 "Content-Type": "application/json"},
        json={"p_jti": str(jti), "p_worker_id": args.worker_id, "p_target": args.target,
              "p_expires_at": expires_at.isoformat(), "p_provider": args.provider},
        timeout=30,
    )
    if response.status_code >= 400:
        sys.exit(f"error: issue_worker_credential failed ({response.status_code}): {response.text[:500]}")

    # 2) Worker JWT. 신원은 전부 클레임에 들어간다 — 워커가 인자로 보내는 값은 서버가 믿지 않는다.
    worker_token = sign_jwt(
        worker_claims(args.worker_id, args.target, args.provider, jti, now, int(expires_at.timestamp())),
        jwt_secret)

    print(f"# worker credential registered: jti={jti}")
    print(f"# worker_id={args.worker_id} target={args.target} provider={args.provider or args.target}")
    print(f"# expires_at={expires_at.isoformat()} ({args.hours}h)")
    print()
    print("# --- paste into the worker .env ---")
    print(f"SUPABASE_URL={supabase_url}")
    print(f"SUPABASE_ANON_KEY={anon_key}")
    print("WORKER_AUTH=worker_jwt")
    print(f"WORKER_JWT={worker_token}")
    print(f"WORKER_ID={args.worker_id}")
    print(f"WORKER_TARGET={args.target}")
    print(f"WORKER_PROVIDER={args.provider or args.target}")
    print("PULL_JOBS=true")
    print("COMFY_URL=http://127.0.0.1:8188")
    print()
    print("# Do NOT copy SUPABASE_JWT_SECRET, SUPABASE_SECRET_KEY or any SNS token into the pod.")
    print(f"# To revoke early: select revoke_worker_credential('{jti}');")


if __name__ == "__main__":
    main()
