"""0008: LLM 하루 호출 한도, 설치된 모델 목록 (TECH_DESIGN 21.18, 22.9, 15.18)."""

from __future__ import annotations

import json

PERMISSION_DENIED = "42501"


def processing_job(db, job_type="prompt"):
    """Operator·Persona·Content Job을 만들고 job_type Job을 선점한 상태로 돌려준다."""
    uid = db.make_operator()
    pid = db.make_persona(uid)
    cj = db.create_content_job(uid, pid)
    db.as_service("n8n")
    db.one("select * from claim_content_job(%s)", (cj["id"],))
    if job_type == "generation":
        job = db.one("select * from create_automation_job(p_job_type => 'generation', p_persona_id => %s,"
                     " p_content_job_id => %s)", (pid, cj["id"]))
        db.as_service("python")
    else:
        job = db.one("select * from create_automation_job(p_job_type => %s, p_persona_id => %s,"
                     " p_content_job_id => %s)", (job_type, pid, cj["id"]))
    claimed = db.one("select * from claim_automation_job(%s, %s)", (job["id"], "n8n"))
    return uid, pid, claimed


def set_llm_limit(db, limit):
    db.as_postgres()
    db.conn.execute(
        "update app_settings set value = jsonb_set(value, '{daily_llm_calls_limit}', to_jsonb(%s::int))"
        " where key = 'limits'", (limit,))


def reserve(db, job, locked_at=None):
    return db.val("select reserve_llm_call(%s, %s)", (job["id"], locked_at or job["locked_at"]))


def test_reserve_llm_call_counts_until_the_daily_limit(db):
    uid, pid, job = processing_job(db)
    set_llm_limit(db, 2)
    db.as_service("n8n")
    assert reserve(db, job) == {"allowed": True, "count": 1, "limit": 2}
    assert reserve(db, job) == {"allowed": True, "count": 2, "limit": 2}
    assert reserve(db, job) == {"allowed": False, "reason": "rate_limited", "limit": 2}
    assert reserve(db, job)["allowed"] is False  # 계속 거부, 카운터는 늘지 않음

    db.as_postgres()
    assert db.val("select count from private.usage_counters where key = 'llm_calls'"
                  " and day = (now() at time zone 'utc')::date") == 2
    events = db.all("select actor_type, persona_id, detail from security_events where event_type = 'RATE_LIMITED'")
    assert len(events) == 2
    assert events[0]["actor_type"] == "n8n" and events[0]["persona_id"] == pid
    assert events[0]["detail"]["automation_job_id"] == str(job["id"])


def test_reserve_llm_call_with_zero_limit_always_denies(db):
    uid, pid, job = processing_job(db)
    set_llm_limit(db, 0)
    db.as_service("n8n")
    assert reserve(db, job) == {"allowed": False, "reason": "rate_limited", "limit": 0}


def test_reserve_llm_call_requires_the_current_lock(db):
    uid, pid, job = processing_job(db)
    db.as_service("n8n")
    stale = db.val("select %s::timestamptz - interval '1 second'", (job["locked_at"],))
    assert reserve(db, job, stale) == {"allowed": False, "reason": "lock_lost"}
    db.as_postgres()
    assert db.val("select count(*) from private.usage_counters") == 0


def test_reserve_llm_call_rejects_generation_jobs(db):
    uid, pid, job = processing_job(db, "generation")
    db.as_service("n8n")
    assert db.expect_error("select reserve_llm_call(%s, %s)", (job["id"], job["locked_at"])).sqlstate == "PT422"


def test_reserve_llm_call_is_worker_only(db):
    uid, pid, job = processing_job(db)
    db.as_operator(uid)
    assert db.expect_error("select reserve_llm_call(%s, %s)", (job["id"], job["locked_at"])).sqlstate == PERMISSION_DENIED
    db.as_anon()
    assert db.expect_error("select reserve_llm_call(%s, %s)", (job["id"], job["locked_at"])).sqlstate == PERMISSION_DENIED


def test_worker_status_models_are_kept_until_replaced(db):
    uid = db.make_operator()
    models = {"checkpoints": ["model_a.safetensors"], "loras": ["gina_v2.safetensors"]}
    db.as_service("python")
    db.val("select report_worker_status('python:rtx5080-1', 'python', %s::jsonb)",
           (json.dumps({"comfyui_ok": True, "models": models}),))
    # ComfyUI가 잠시 꺼져 목록 없이 보고해도 이전 목록은 남는다
    db.val("select report_worker_status('python:rtx5080-1', 'python', %s::jsonb)",
           (json.dumps({"comfyui_ok": False}),))

    db.as_operator(uid)
    row = db.one("select comfyui_ok, models from worker_status where id = 'python:rtx5080-1'")
    assert row == {"comfyui_ok": False, "models": models}

    db.as_service("python")
    newer = {"checkpoints": ["model_b.safetensors"], "loras": []}
    db.val("select report_worker_status('python:rtx5080-1', 'python', %s::jsonb)", (json.dumps({"models": newer}),))
    db.as_operator(uid)
    assert db.val("select models from worker_status where id = 'python:rtx5080-1'") == newer


def test_worker_status_models_must_be_lists_in_an_object(db):
    db.as_service("python")
    for bad in (["model_a.safetensors"], {"checkpoints": "model_a.safetensors"}, {"loras": {"a": 1}}):
        err = db.expect_error("select report_worker_status('python:rtx5080-1', 'python', %s::jsonb)",
                              (json.dumps({"models": bad}),))
        assert err.sqlstate == "PT422", bad
