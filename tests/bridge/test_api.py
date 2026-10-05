"""Bridge API /v1 (TECH_DESIGN 12.6, 15.8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.bridge.conftest import TOKEN, make_settings

AUTH = {"X-Bridge-Token": TOKEN}


@pytest.fixture
def client(env):
    app = create_app(env.settings, repo=env.repo, storage=env.storage, comfy=env.comfy.client(),
                     notifier=env.notifier, start_background=False)
    with TestClient(app) as c:
        c.env = env
        yield c


def pending_job(env):
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    return env.seed.generation_job(uid, persona["id"])


def test_claims_job_and_queues_it(client):
    ids = pending_job(client.env)
    r = client.post("/v1/jobs", json={"job_id": ids["job_id"]}, headers=AUTH)
    assert r.status_code == 202, r.text
    assert r.json() == {"accepted": True, "job_id": ids["job_id"], "queue_position": 1}
    assert client.app.state.worker.queued_ids == [ids["job_id"]]
    row = client.env.seed.as_postgres().one("select status, attempts, claimed_by from automation_jobs where id = %s",
                                           (ids["job_id"],))
    assert row == {"status": "processing", "attempts": 1, "claimed_by": "python:rtx5080-1"}
    # 같은 Job을 다시 보내면 409
    again = client.post("/v1/jobs", json={"job_id": ids["job_id"]}, headers=AUTH)
    assert again.status_code == 409 and again.json()["error"]["code"] == "JOB_NOT_CLAIMABLE"


def test_claims_next_job_without_body(client):
    ids = pending_job(client.env)
    r = client.post("/v1/jobs", headers=AUTH)
    assert r.status_code == 202 and r.json()["job_id"] == ids["job_id"]
    assert client.post("/v1/jobs", headers=AUTH).status_code == 409  # 더 없음


def test_comfy_down_returns_503_without_claiming(client):
    ids = pending_job(client.env)
    client.env.comfy.reachable = False
    r = client.post("/v1/jobs", json={"job_id": ids["job_id"]}, headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["code"] == "COMFY_UNAVAILABLE"
    row = client.env.seed.as_postgres().one("select status, attempts from automation_jobs where id = %s", (ids["job_id"],))
    assert row == {"status": "pending", "attempts": 0}  # 재시도 횟수를 쓰지 않았다


@pytest.mark.parametrize("body", ['{"job_id": "not-a-uuid"}', '{"job_id": null, "x": 1}', "[1]", "{bad json"])
def test_invalid_body_is_422(client, body):
    r = client.post("/v1/jobs", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"


def test_body_too_large_is_413(client):
    r = client.post("/v1/jobs", content="x" * 5000, headers=AUTH)
    assert r.status_code == 413


def test_non_generation_job_is_rejected(client):
    env = client.env
    uid = env.seed.operator()
    persona = env.seed.persona(uid)
    ids = env.seed.generation_job(uid, persona["id"])
    prompt_job = env.seed.as_postgres().one(
        "insert into automation_jobs (persona_id, content_job_id, job_type, worker) values (%s, %s, 'caption', 'n8n')"
        " returning id::text as id", (persona["id"], ids["content_job_id"]))
    r = client.post("/v1/jobs", json={"job_id": prompt_job["id"]}, headers=AUTH)
    assert r.status_code == 422


def test_unknown_job_is_409(client):
    r = client.post("/v1/jobs", json={"job_id": "00000000-0000-0000-0000-000000000000"}, headers=AUTH)
    assert r.status_code == 409


def test_bad_tokens_are_rejected_logged_and_blocked(client):
    for _ in range(10):  # 1분에 10회까지는 거부만
        assert client.post("/v1/jobs", headers={"X-Bridge-Token": "wrong"}).status_code == 401
    assert client.post("/v1/jobs").status_code == 401                # 11번째 → 이번에 차단됨
    assert client.post("/v1/jobs", headers=AUTH).status_code == 429  # 이제 올바른 토큰도 막힘
    # 인증 없는 요청마다 DB에 쓰지 않는다: 첫 실패와 차단 시점만 기록
    events = client.env.seed.as_postgres().all("select event_type, detail from security_events order by id")
    assert [e["detail"]["blocked"] for e in events] == [False, True]
    assert all(e["event_type"] == "API_AUTH_FAILED" for e in events)


def test_invalid_content_length_is_rejected(client):
    r = client.post("/v1/jobs", headers={**AUTH, "Content-Length": "abc"}, content=b"")
    assert r.status_code in (400, 413)


def test_token_rotation_accepts_old_and_new(env):
    old, new = "o" * 40, "n" * 40
    env.settings = make_settings(bridge_tokens=(new, old))
    app = create_app(env.settings, repo=env.repo, storage=env.storage, comfy=env.comfy.client(),
                     notifier=env.notifier, start_background=False)
    with TestClient(app) as c:
        for token in (new, old):
            assert c.get("/v1/status", headers={"X-Bridge-Token": token}).status_code == 200


def test_health_is_public_and_minimal(client):
    r = client.get("/v1/health")
    assert r.status_code == 200 and set(r.json()) == {"ok", "comfyui", "queue_size", "busy"}


def test_status_requires_token(client):
    assert client.get("/v1/status").status_code == 401
    r = client.get("/v1/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["worker_id"] == "python:rtx5080-1" and body["workflows"]["faceswap_v1"]["enabled"] is False


def test_cancel_removes_queued_job(client):
    ids = pending_job(client.env)
    client.post("/v1/jobs", json={"job_id": ids["job_id"]}, headers=AUTH)
    assert client.post(f"/v1/jobs/{ids['job_id']}/cancel", headers=AUTH).json() == {"cancelled": True}
    assert client.app.state.worker.queued_ids == []
    assert client.post(f"/v1/jobs/{ids['job_id']}/cancel", headers=AUTH).status_code == 404


def test_no_public_docs(client):
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path).status_code == 404
