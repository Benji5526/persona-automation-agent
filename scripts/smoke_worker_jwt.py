"""호스팅 Supabase에서 Worker JWT 경로를 실제로 검증하는 스모크 테스트 (TECH_DESIGN 56.5, 최종 게이트 2·3번).

이 스크립트는 **로컬 테스트로 검증할 수 없는 것**을 확인한다: 호스팅 PostgREST/Storage가 `role = pa_worker` JWT를 받아
authenticator → pa_worker로 전환하는지. 소유자 PC에서 실제 프로젝트에 대해 실행한다. service_role 키를 쓰지 않는다.

  python scripts/smoke_worker_jwt.py            # 실제 실행
  python scripts/smoke_worker_jwt.py --dry-run  # 보낼 요청만 출력

필요한 환경변수 (mint_worker_token.py와 같다)
  SUPABASE_URL · SUPABASE_ANON_KEY · SUPABASE_JWT_SECRET · PA_OWNER_USER_ID

결과는 항목마다 PASS / FAIL / BLOCKED 다.
  PASS    기대한 대로 동작했다
  FAIL    보안 경계가 깨졌다 (거부돼야 할 것이 허용됐다). **Cloud를 켜면 안 된다.**
  BLOCKED 플랫폼이 이 JWT 방식을 받아들이지 않는다 (JWT 서명 키 설정·역할 전환). 원인을 출력한다.
          승인된 fallback을 쓴다. 절대 service_role로 대체하지 않는다.

해석할 때 알아 둘 것
  * Supabase가 새 JWT Signing Keys(비대칭)만 쓰고 레거시 HS256 secret을 끈 프로젝트에서는, 이 스크립트가 만든 HS256 토큰이
    거부될 수 있다. 그건 BLOCKED이고 기록해 둘 사실이다 (레거시 secret 유지 또는 직접 가져온 서명 키로 서명해야 한다).
  * Storage 쓰기 허용 경로(선점한 Job의 Persona)는 선점된 cloud Job이 있어야 확인할 수 있다 → 마지막에 수동 절차를 출력한다.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mint_worker_token import sign_jwt, worker_claims  # noqa: E402

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


@dataclass
class Config:
    url: str
    anon_key: str
    jwt_secret: str
    owner_id: str


def error_code(response: httpx.Response) -> str:
    """응답에서 사람이 읽을 사유를 뽑는다. 정상 응답은 JSON 배열일 수 있다 (행 목록 · 빈 배열)."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:120]
    if isinstance(body, dict):
        return f"{body.get('code', '')} {body.get('message', '')}".strip()
    return f"{len(body)} row(s)" if isinstance(body, list) else str(body)[:120]


class Smoke:
    def __init__(self, client: httpx.Client, cfg: Config):
        self.http, self.cfg = client, cfg
        self.results: list[Result] = []
        self.now = int(time.time())

    # -- 도우미 -------------------------------------------------------------------
    def owner_headers(self) -> dict:
        token = sign_jwt({"sub": self.cfg.owner_id, "role": "authenticated", "iat": self.now, "exp": self.now + 300},
                         self.cfg.jwt_secret)
        return {"apikey": self.cfg.anon_key, "Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def worker_headers(self, claims: dict) -> dict:
        return {"apikey": self.cfg.anon_key, "Authorization": f"Bearer {sign_jwt(claims, self.cfg.jwt_secret)}",
                "Content-Type": "application/json"}

    def rpc(self, name: str, headers: dict, body: dict | None = None) -> httpx.Response:
        return self.http.post(f"{self.cfg.url}/rest/v1/rpc/{name}", headers=headers, json=body or {}, timeout=30)

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(Result(name, status, detail))

    def issue(self, worker_id: str, target: str, hours: float = 1.0) -> tuple[uuid.UUID, dict]:
        jti = uuid.uuid4()
        expires = datetime.now(timezone.utc) + timedelta(hours=hours)
        r = self.rpc("issue_worker_credential", self.owner_headers(), {
            "p_jti": str(jti), "p_worker_id": worker_id, "p_target": target,
            "p_expires_at": expires.isoformat(), "p_provider": "smoke"})
        if r.status_code >= 400:
            raise RuntimeError(f"issue_worker_credential failed ({r.status_code}): {error_code(r)}")
        return jti, worker_claims(worker_id, target, "smoke", jti, self.now, int(expires.timestamp()))

    # -- 점검 ---------------------------------------------------------------------
    def run(self) -> list[Result]:
        worker_id = f"python:smoke-{uuid.uuid4().hex[:6]}"
        try:
            jti, claims = self.issue(worker_id, "cloud")
        except Exception as exc:  # 소유자 인증·발급이 안 되면 나머지를 판단할 수 없다
            self.record("owner can issue a worker credential", BLOCKED, str(exc))
            return self.results
        self.record("owner can issue a worker credential", PASS, f"jti={jti}")
        hdr = self.worker_headers(claims)

        # 1) 역할 매핑: 허용된 RPC가 200이어야 한다. 여기서 막히면 플랫폼이 pa_worker 역할 전환을 받아들이지 않는 것이다.
        r = self.rpc("claim_next_automation_job", hdr, {"p_job_type": "generation", "p_worker": "ignored"})
        if r.status_code == 200:
            self.record("JWT role=pa_worker is accepted and may call an allowed RPC", PASS)
        else:
            self.record("JWT role=pa_worker is accepted and may call an allowed RPC", BLOCKED,
                        f"HTTP {r.status_code}: {error_code(r)}")
            return self.results       # 매핑이 안 되면 아래 거부 점검은 의미가 없다

        # 2) 테이블 직접 접근은 없다
        for table in ("automation_jobs", "social_accounts", "personas", "app_settings", "worker_credentials"):
            r = self.http.get(f"{self.cfg.url}/rest/v1/{table}?limit=1", headers=hdr, timeout=30)
            denied = r.status_code in (401, 403) or "42501" in error_code(r)
            self.record(f"worker cannot read table {table}", PASS if denied else FAIL,
                        f"HTTP {r.status_code}: {error_code(r)}")

        # 3) 허용 목록 밖 RPC는 거부된다
        for name in ("create_content_job", "update_app_setting", "get_app_settings", "issue_worker_credential",
                     "move_pending_jobs_to_target", "sync_workflow_registry", "recover_stale_jobs"):
            r = self.rpc(name, hdr, {})
            denied = r.status_code in (401, 403, 404) or "42501" in error_code(r) or "PGRST202" in error_code(r)
            self.record(f"worker cannot call {name}", PASS if denied else FAIL, f"HTTP {r.status_code}: {error_code(r)}")

        # 4) 다른 워커를 사칭할 수 없다
        r = self.rpc("report_worker_status", hdr, {"p_worker_id": "python:someone-else", "p_kind": "python", "p_info": {}})
        self.record("worker cannot report status for another worker",
                    PASS if r.status_code == 403 else FAIL, f"HTTP {r.status_code}: {error_code(r)}")

        # 5) 만료된 토큰은 PostgREST가 거부한다
        expired = {**claims, "iat": self.now - 7200, "exp": self.now - 3600}
        r = self.rpc("claim_next_automation_job", self.worker_headers(expired), {"p_job_type": "generation", "p_worker": "x"})
        self.record("expired token is rejected", PASS if r.status_code in (401, 403) else FAIL,
                    f"HTTP {r.status_code}: {error_code(r)}")

        # 6) Storage: 선점한 Job이 없으면 어떤 경로도 쓸 수 없고, 목록도 비어 있다
        path = f"persona/{uuid.uuid4()}/assets/{uuid.uuid4()}.png"
        r = self.http.post(f"{self.cfg.url}/storage/v1/object/media/{path}", headers={**hdr, "Content-Type": "image/png"},
                           content=b"\x89PNG", timeout=30)
        self.record("storage: worker without a claimed job cannot write", PASS if r.status_code in (400, 401, 403) else FAIL,
                    f"HTTP {r.status_code}: {error_code(r)}")
        r = self.http.post(f"{self.cfg.url}/storage/v1/object/list/media", headers=hdr,
                           json={"prefix": "", "limit": 100}, timeout=30)
        listing_empty = r.status_code in (400, 401, 403) or (r.status_code == 200 and r.json() == [])
        self.record("storage: worker cannot enumerate the media bucket", PASS if listing_empty else FAIL,
                    f"HTTP {r.status_code}")

        # 7) 폐기하면 다음 RPC부터 실패한다
        r = self.rpc("revoke_worker_credential", self.owner_headers(), {"p_jti": str(jti)})
        if r.status_code >= 400:
            self.record("owner can revoke the credential", BLOCKED, f"HTTP {r.status_code}: {error_code(r)}")
            return self.results
        self.record("owner can revoke the credential", PASS)
        r = self.rpc("claim_next_automation_job", hdr, {"p_job_type": "generation", "p_worker": "ignored"})
        refused = r.status_code == 403 and "worker credential" in (r.text or "")
        self.record("revoked credential is refused on the next RPC", PASS if refused else FAIL,
                    f"HTTP {r.status_code}: {error_code(r)}")
        return self.results


MANUAL = """
수동으로 이어서 확인할 것 (선점된 cloud Job이 필요하다)
  1. Lovable 또는 소유자가 cloud Job 하나를 만든다 (create_content_job의 p_execution_target = 'cloud').
  2. 새 Worker JWT를 발급한다 (scripts/mint_worker_token.py ... --target cloud) 하고, 그 토큰으로 claim_next_automation_job을 호출해 Job을 선점한다.
  3. 그 토큰으로 Storage에 persona/{그 Job의 persona}/assets/{새 uuid}.png 를 올린다 → 성공해야 한다.
     같은 이름으로 다시 올리거나(덮어쓰기), 다른 Persona 경로·다른 확장자·'..' 경로로 올리면 → 거부돼야 한다.
  4. 같은 토큰으로 입력 파일 읽기: 그 Job이 입력으로 참조한 파일만 읽히고, 같은 Persona의 다른 파일은 안 보여야 한다.
  5. 막히면(예: Storage API가 bucket 조회에 storage.buckets SELECT를 요구) 그 권한 하나만 새 마이그레이션(0012)으로 추가하고
     권한 표면 테스트(tests/db/test_permission_surface.py)를 같이 고친다. service_role로 대체하지 않는다.
"""


def summarize(results: list[Result]) -> int:
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"[{r.status:7}] {r.name.ljust(width)}  {r.detail}")
    fails = [r for r in results if r.status == FAIL]
    blocked = [r for r in results if r.status == BLOCKED]
    print()
    if fails:
        print(f"FAIL {len(fails)}: 보안 경계가 깨졌다. Cloud를 켜지 않는다.")
        return 2
    if blocked:
        print(f"BLOCKED {len(blocked)}: 플랫폼이 이 방식을 받아들이지 않는다. 위 사유를 기록하고 승인된 fallback을 쓴다.")
        return 3
    print("모두 PASS. 아래 수동 절차까지 끝나야 Cloud 활성화 조건이 충족된다.")
    print(MANUAL)
    return 0


def load_config() -> Config:
    missing = [n for n in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_JWT_SECRET", "PA_OWNER_USER_ID")
               if not os.environ.get(n, "").strip()]
    if missing:
        sys.exit(f"error: set {', '.join(missing)} (see the docstring). service_role keys are not used.")
    return Config(os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_ANON_KEY"].strip(),
                  os.environ["SUPABASE_JWT_SECRET"].strip(), os.environ["PA_OWNER_USER_ID"].strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="요청을 보내지 않고 점검 항목만 출력한다")
    args = parser.parse_args()
    if args.dry_run:
        print("점검 항목(순서대로): 자격 발급 → 허용 RPC 200(역할 매핑) → 테이블 5종 거부 → 허용 목록 밖 RPC 7종 거부 → "
              "다른 워커 사칭 거부 → 만료 토큰 거부 → Storage 쓰기·목록 거부 → 폐기 후 거부")
        print(MANUAL)
        return 0
    with httpx.Client() as client:
        return summarize(Smoke(client, load_config()).run())


if __name__ == "__main__":
    sys.exit(main())
