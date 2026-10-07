"""0010 execution_targets (TECH_DESIGN 56.2, 56.4): 활성 워커, worker_status.target·provider, 선점 gating."""

from __future__ import annotations

import json

import pytest

from tests.db.test_db import create_job, to_generating

PERMISSION_DENIED = "42501"


@pytest.fixture
def admin(db):
    uid = db.make_operator()
    db.as_postgres()
    db.conn.execute("update users set role = 'admin' where id = %s", (uid,))
    return uid


def report(db, worker_id, kind="python", **info):
    db.as_service("python" if kind == "python" else "n8n")
    db.val("select report_worker_status(%s, %s, %s::jsonb)", (worker_id, kind, json.dumps(info)))


def set_active(db, uid, value):
    db.as_operator(uid)
    return db.val("select update_app_setting('active_worker', %s::jsonb)", (json.dumps(value),))


def pending_generation_job(db, uid):
    pid = db.make_persona(uid)
    cj = to_generating(db, uid, pid)
    job = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
    return pid, cj, job


# -----------------------------------------------------------------------------
# 설정 기본값과 노출
# -----------------------------------------------------------------------------
def test_defaults_keep_current_behavior(db, admin):
    db.as_postgres()
    assert db.val("select value = 'null'::jsonb from app_settings where key = 'active_worker'") is True  # 행이 있고 jsonb null (제한 없음)
    assert db.val("select value from app_settings where key = 'app_mode'") == "personal"
    db.as_operator(admin)
    settings = db.val("select get_app_settings()")
    assert settings["app_mode"] == "personal" and settings["active_worker"] is None


def test_app_mode_is_read_only_through_the_settings_rpc(db, admin):
    db.as_operator(admin)
    err = db.expect_error("select update_app_setting('app_mode', '\"saas\"'::jsonb)")
    assert err.sqlstate == "PT422" and "cannot be changed" in str(err)


# -----------------------------------------------------------------------------
# worker_status: target·provider
# -----------------------------------------------------------------------------
def test_worker_reports_target_and_provider_and_keeps_previous_when_absent(db, admin):
    report(db, "python:local-1", target="local", provider="local", gpu={"name": "RTX 3070 Laptop", "vram_total_mb": 8192})
    report(db, "python:local-1", queue_size=1, gpu={"name": "RTX 3070 Laptop", "vram_total_mb": 8192})  # target·provider 없음
    db.as_operator(admin)
    row = db.one("select target, provider, gpu, queue_size from worker_status where id = 'python:local-1'")
    assert (row["target"], row["provider"], row["queue_size"]) == ("local", "local", 1)
    assert row["gpu"]["name"] == "RTX 3070 Laptop"          # GPU 모델은 설정이 아니라 보고값


def test_worker_status_rejects_invalid_target_or_provider(db):
    db.as_service("python")
    for info in ({"target": "mars"}, {"provider": "Bad Provider!"}):
        err = db.expect_error("select report_worker_status('python:x', 'python', %s::jsonb)", (json.dumps(info),))
        assert err.sqlstate == "PT422", info


# -----------------------------------------------------------------------------
# active_worker 변경 규칙
# -----------------------------------------------------------------------------
def test_only_admin_can_change_active_worker(db):
    uid = db.make_operator()          # 일반 operator
    report(db, "python:local-1", target="local")
    db.as_operator(uid)
    assert db.expect_error("select update_app_setting('active_worker', '\"python:local-1\"'::jsonb)").sqlstate == "PT403"


def test_active_worker_must_be_a_registered_python_worker_or_null(db, admin):
    report(db, "python:local-1", target="local")
    report(db, "n8n:main", kind="n8n")
    db.as_operator(admin)
    for bad in ('"python:unknown"', '"n8n:main"', "5", "true", "[]"):
        assert db.expect_error("select update_app_setting('active_worker', %s::jsonb)", (bad,)).sqlstate == "PT422", bad

    assert set_active(db, admin, "python:local-1")["active_worker"] == "python:local-1"
    assert set_active(db, admin, None)["active_worker"] is None            # jsonb null
    assert set_active(db, admin, "python:local-1")["active_worker"] == "python:local-1"
    db.as_operator(admin)
    # PostgREST는 JSON null을 SQL NULL로 넘긴다
    assert db.val("select update_app_setting('active_worker', null)")["active_worker"] is None


def test_active_worker_change_is_audited(db, admin):
    report(db, "python:cloud-1", target="cloud", provider="runpod")
    set_active(db, admin, "python:cloud-1")
    db.as_postgres()
    event = db.one("select event_type, detail from security_events where actor_id = %s order by id desc limit 1", (admin,))
    assert event["event_type"] == "ACTIVE_WORKER_CHANGED" and event["detail"]["worker"] == "python:cloud-1"


# -----------------------------------------------------------------------------
# 선점 gating (generation만)
# -----------------------------------------------------------------------------
def test_without_active_worker_any_worker_can_claim(db, admin):
    _, _, job = pending_generation_job(db, admin)
    db.as_service("python")
    row = db.one("select * from claim_next_automation_job('generation', 'python:anyone')")
    assert row["id"] == job["id"] and row["claimed_by"] == "python:anyone"


def test_only_the_active_worker_can_claim_generation_jobs(db, admin):
    report(db, "python:local-1", target="local")
    report(db, "python:cloud-1", target="cloud", provider="runpod")
    _, _, job = pending_generation_job(db, admin)
    set_active(db, admin, "python:cloud-1")

    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:local-1')") is None
    assert db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)) is None
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "pending"  # attempts도 그대로
    assert db.val("select attempts from automation_jobs where id = %s", (job["id"],)) == 0

    row = db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (job["id"],))
    assert row["status"] == "processing" and row["claimed_by"] == "python:cloud-1"


def test_switching_the_active_worker_moves_pending_jobs(db, admin):
    report(db, "python:local-1", target="local")
    report(db, "python:cloud-1", target="cloud", provider="runpod")
    _, _, job = pending_generation_job(db, admin)
    set_active(db, admin, "python:local-1")
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:cloud-1')") is None

    set_active(db, admin, "python:cloud-1")                  # 전환: 대기 중인 Job을 새 워커가 가져간다
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:local-1')") is None
    assert db.one("select * from claim_next_automation_job('generation', 'python:cloud-1')")["id"] == job["id"]


def test_gating_does_not_affect_other_job_types_or_running_jobs(db, admin):
    report(db, "python:local-1", target="local")
    report(db, "python:cloud-1", target="cloud")
    pid = db.make_persona(admin)
    cj = to_generating(db, admin, pid)
    gen = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
    db.as_service("python")
    running = db.one("select * from claim_automation_job(%s, 'python:local-1')", (gen["id"],))
    set_active(db, admin, "python:cloud-1")                  # 실행 중에 전환해도

    db.as_service("python")
    assert db.val("select heartbeat_automation_job(%s, %s)", (running["id"], running["locked_at"])) is True  # 계속 진행

    # prompt 같은 n8n Job은 active_worker와 상관없이 선점된다
    cj2 = to_generating(db, admin, pid)
    pj = create_job(db, "prompt", pid, cj2["id"], key=f"prompt:{cj2['id']}:1")
    db.as_service("n8n")
    assert db.one("select * from claim_automation_job(%s, 'n8n')", (pj["id"],))["status"] == "processing"
    cj3 = db.create_content_job(admin, pid, p_submit=True)
    db.as_service("n8n")
    assert db.one("select * from claim_content_job(%s)", (cj3["id"],)) is not None


def test_null_worker_id_cannot_pass_an_active_worker_restriction(db, admin):
    report(db, "python:cloud-1", target="cloud")
    _, _, job = pending_generation_job(db, admin)
    set_active(db, admin, "python:cloud-1")
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', null)") is None
    assert db.one("select * from claim_automation_job(%s, null)", (job["id"],)) is None


def test_non_generation_claim_next_ignores_active_worker(db, admin):
    report(db, "python:cloud-1", target="cloud")
    pid = db.make_persona(admin)
    cj = to_generating(db, admin, pid)
    pj = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:1")
    set_active(db, admin, "python:cloud-1")
    db.as_service("n8n")
    assert db.one("select * from claim_next_automation_job('prompt', 'n8n')")["id"] == pj["id"]


def test_non_string_active_worker_value_means_no_restriction(db, admin):
    """직접 쓴 비정상 값(문자열이 아님)은 제한 없음으로 본다 (RPC는 이런 값을 받지 않는다)."""
    _, _, job = pending_generation_job(db, admin)
    db.as_postgres()
    db.conn.execute("update app_settings set value = '5'::jsonb where key = 'active_worker'")
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:any')")["id"] == job["id"]
