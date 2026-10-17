"""scripts/smoke_worker_jwt.py의 판정 로직 검증 (최종 게이트 2번).

주의: 이 테스트는 **스모크 스크립트가 올바르게 판정한다**는 것만 확인한다. 호스팅 Supabase가 실제로 role=pa_worker를
받아들이는지는 아니다 — 그건 실제 프로젝트에서 스크립트를 실행해야만 알 수 있다 (NOT VERIFIED).
모의 서버가 세 가지 플랫폼 행동을 흉내 낸다: 올바른 플랫폼 / 역할 전환을 거부하는 플랫폼 / 경계가 새는 플랫폼.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("smoke_worker_jwt", ROOT / "scripts" / "smoke_worker_jwt.py")
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke_worker_jwt"] = smoke          # dataclass가 모듈을 찾을 수 있게 등록한다
_spec.loader.exec_module(smoke)

CFG = smoke.Config(url="https://ref.supabase.co", anon_key="anon_public", jwt_secret="test-signing-secret",
                   owner_id="11111111-1111-4111-8111-111111111111")
ALLOWED = {"claim_next_automation_job", "report_worker_status"}


def claims_of(request: httpx.Request) -> dict:
    token = request.headers["authorization"].split()[-1]
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def fake_supabase(*, maps_role=True, leaky=False):
    revoked: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path, claims = request.url.path, claims_of(request)
        is_owner = claims.get("role") == "authenticated"
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else {}

        if path.endswith("/rpc/issue_worker_credential"):
            return httpx.Response(200 if is_owner else 403, json={})
        if path.endswith("/rpc/revoke_worker_credential"):
            revoked.add(body["p_jti"])
            return httpx.Response(200, json={})

        # 여기부터 워커 요청
        if claims.get("exp", 0) <= time.time():
            return httpx.Response(401, json={"code": "PGRST301", "message": "JWT expired"})
        if not maps_role:
            return httpx.Response(400, json={"code": "42704", "message": 'role "pa_worker" does not exist'})
        if claims.get("jti") in revoked:
            return httpx.Response(
                403, text='{"code":"PT403","message":"FORBIDDEN","details":"worker credential is missing, expired or revoked"}')

        if "/rest/v1/rpc/" in path:
            name = path.rsplit("/", 1)[-1]
            if name not in ALLOWED:
                return httpx.Response(403, json={"code": "42501", "message": "permission denied for function"})
            if name == "report_worker_status" and body.get("p_worker_id") != claims["worker_id"]:
                return httpx.Response(403, json={"code": "PT403", "message": "FORBIDDEN"})
            return httpx.Response(200, json=[])
        if path.startswith("/rest/v1/"):
            return httpx.Response(200, json=[{"leak": True}]) if leaky else \
                httpx.Response(403, json={"code": "42501", "message": "permission denied for table"})
        if path.startswith("/storage/v1/object/list/"):
            return httpx.Response(200, json=[])
        if path.startswith("/storage/v1/object/"):
            return httpx.Response(403, json={"statusCode": "403", "message": "new row violates row-level security policy"})
        return httpx.Response(404, json={})

    return handler


def run(**flags):
    with httpx.Client(transport=httpx.MockTransport(fake_supabase(**flags))) as client:
        return smoke.Smoke(client, CFG).run()


def statuses(results):
    return {r.name: r.status for r in results}


def test_a_correct_platform_passes_every_check():
    results = run()
    assert {r.status for r in results} == {smoke.PASS}, [r for r in results if r.status != smoke.PASS]
    assert smoke.summarize(results) == 0
    assert len(results) >= 20


def test_a_platform_that_rejects_the_pa_worker_role_is_reported_as_blocked_with_its_reason():
    results = run(maps_role=False)
    by = statuses(results)
    assert by["JWT role=pa_worker is accepted and may call an allowed RPC"] == smoke.BLOCKED
    assert "does not exist" in next(r.detail for r in results if r.status == smoke.BLOCKED and "accepted" in r.name)
    assert smoke.summarize(results) == 3                      # BLOCKED → 종료 코드 3 (PASS처럼 보이지 않는다)
    assert smoke.FAIL not in by.values()                     # 거부 점검을 의미 없이 PASS로 채우지 않는다


def test_a_platform_that_leaks_a_table_is_reported_as_a_failure():
    results = run(leaky=True)
    failed = [r.name for r in results if r.status == smoke.FAIL]
    assert any("cannot read table" in n for n in failed)
    assert smoke.summarize(results) == 2                      # 보안 경계 위반은 가장 높은 종료 코드


def test_the_smoke_test_never_uses_a_service_role_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return fake_supabase()(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        smoke.Smoke(client, CFG).run()
    assert seen and all(h["apikey"] == "anon_public" for h in seen)
    assert all(claims_of(httpx.Request("GET", "https://x", headers=h)).get("role") != "service_role" for h in seen)


def test_the_dry_run_prints_the_manual_procedure(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["smoke", "--dry-run"])
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "수동으로 이어서 확인할 것" in out and "service_role로 대체하지 않는다" in out
