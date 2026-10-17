"""0010 execution_targets (TECH_DESIGN 56): Execution Target 고정·상속·선점 gating·이동·재시도.

Target(어디서) · Worker(누가) · Provider(인프라)는 서로 다른 개념이다. 하나로 합치지 않는다.
"""

from __future__ import annotations

import json

import pytest

from tests.db.test_db import claim, create_job, to_generating

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


def set_default_target(db, uid, value):
    db.as_operator(uid)
    return db.val("select update_app_setting('default_execution_target', %s::jsonb)", (json.dumps(value),))


def pending_generation_job(db, uid, **kwargs):
    """generating 상태의 Content Job 아래 pending generation Job을 만든다."""
    pid = db.make_persona(uid)
    cj = to_generating(db, uid, pid, **kwargs)
    job = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
    return pid, cj, job


def target_of(db, job_id):
    db.as_postgres()
    return db.val("select execution_target from automation_jobs where id = %s", (job_id,))


# -----------------------------------------------------------------------------
# 설정: 기본값과 변경 규칙
# -----------------------------------------------------------------------------
def test_defaults_are_personal_and_local(db, admin):
    db.as_postgres()
    assert db.val("select value from app_settings where key = 'app_mode'") == "personal"
    assert db.val("select value from app_settings where key = 'default_execution_target'") == "local"
    assert db.val("select count(*) from app_settings where key = 'active_worker'") == 0  # 구 설계는 남지 않는다

    db.as_operator(admin)
    settings = db.val("select get_app_settings()")
    assert settings["app_mode"] == "personal" and settings["default_execution_target"] == "local"
    assert "active_worker" not in settings


def test_app_mode_is_read_only_through_the_settings_rpc(db, admin):
    db.as_operator(admin)
    err = db.expect_error("select update_app_setting('app_mode', '\"saas\"'::jsonb)")
    assert err.sqlstate == "PT422" and "cannot be changed" in str(err)


def test_only_admin_can_change_the_default_target(db):
    uid = db.make_operator()  # 일반 operator
    db.as_operator(uid)
    assert db.expect_error(
        "select update_app_setting('default_execution_target', '\"cloud\"'::jsonb)"
    ).sqlstate == "PT403"


def test_default_target_must_be_local_or_cloud(db, admin):
    db.as_operator(admin)
    for bad in ('"mars"', '"LOCAL"', "5", "true", "[]", "null"):
        assert db.expect_error(
            "select update_app_setting('default_execution_target', %s::jsonb)", (bad,)
        ).sqlstate == "PT422", bad
    assert set_default_target(db, admin, "cloud")["default_execution_target"] == "cloud"
    assert set_default_target(db, admin, "local")["default_execution_target"] == "local"


def test_default_target_change_is_audited(db, admin):
    set_default_target(db, admin, "cloud")
    db.as_postgres()
    event = db.one(
        "select event_type, detail from security_events where actor_id = %s order by id desc limit 1", (admin,)
    )
    assert event["event_type"] == "DEFAULT_EXECUTION_TARGET_CHANGED" and event["detail"]["target"] == "cloud"


# -----------------------------------------------------------------------------
# worker_status: target·provider (Worker와 Provider는 Target과 별개다)
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
# Target은 Content Job 생성 시점에 고정된다
# -----------------------------------------------------------------------------
def test_content_job_takes_the_default_target(db, admin):
    pid = db.make_persona(admin)
    assert db.create_content_job(admin, pid)["execution_target"] == "local"

    set_default_target(db, admin, "cloud")
    assert db.create_content_job(admin, pid)["execution_target"] == "cloud"


def test_content_job_accepts_an_explicit_target_override(db, admin):
    pid = db.make_persona(admin)
    assert db.create_content_job(admin, pid, p_execution_target="cloud")["execution_target"] == "cloud"

    set_default_target(db, admin, "cloud")
    assert db.create_content_job(admin, pid, p_execution_target="local")["execution_target"] == "local"


def test_content_job_rejects_an_invalid_target(db, admin):
    pid = db.make_persona(admin)
    db.as_operator(admin)
    for bad in ("mars", "LOCAL", "", "local; drop table users"):
        err = db.expect_error(
            "select * from create_content_job(p_persona_id => %s, p_content_type => 'image',"
            " p_execution_target => %s)",
            (pid, bad),
        )
        assert err.sqlstate == "PT422", bad


def test_operator_cannot_set_a_target_by_inserting_directly(db, admin):
    """직접 INSERT는 Target 컬럼 권한이 없다 → RPC를 우회해 cloud로 만들 수 없다 (3.2)."""
    pid = db.make_persona(admin)
    db.as_operator(admin)
    err = db.expect_error(
        "insert into content_jobs (persona_id, content_type, execution_target) values (%s, 'image', 'cloud')",
        (pid,),
    )
    assert err.sqlstate == PERMISSION_DENIED

    # 권한이 있는 컬럼만 쓴 직접 INSERT는 기본값을 받는다
    db.as_operator(admin)
    db.conn.execute("insert into content_jobs (persona_id, content_type) values (%s, 'image')", (pid,))
    db.as_postgres()
    assert db.val(
        "select execution_target from content_jobs where persona_id = %s order by created_at desc limit 1", (pid,)
    ) == "local"


def test_automation_job_inherits_the_content_job_target(db, admin):
    _, cj, job = pending_generation_job(db, admin, p_execution_target="cloud")
    assert cj["execution_target"] == "cloud"
    assert target_of(db, job["id"]) == "cloud"


def test_automation_job_target_cannot_diverge_from_the_content_job(db, admin):
    """직접 UPDATE로 어긋나게 해도 상속 트리거가 상위 값으로 되돌린다."""
    _, cj, job = pending_generation_job(db, admin, p_execution_target="local")
    db.as_postgres()
    db.conn.execute("update automation_jobs set execution_target = 'cloud' where id = %s", (job["id"],))
    assert target_of(db, job["id"]) == "local"


def test_changing_the_default_does_not_move_existing_jobs(db, admin):
    _, cj, job = pending_generation_job(db, admin)
    assert target_of(db, job["id"]) == "local"

    set_default_target(db, admin, "cloud")          # 기본값만 바뀐다

    db.as_postgres()
    assert db.val("select execution_target from content_jobs where id = %s", (cj["id"],)) == "local"
    assert target_of(db, job["id"]) == "local"


# -----------------------------------------------------------------------------
# 선점: Job의 Target과 워커의 Target이 같아야 한다 (generation만)
# -----------------------------------------------------------------------------
def test_local_worker_claims_a_local_job(db, admin):
    report(db, "python:local-1", target="local", provider="local")
    _, _, job = pending_generation_job(db, admin)
    db.as_service("python")
    row = db.one("select * from claim_next_automation_job('generation', 'python:local-1')")
    assert row["id"] == job["id"] and row["execution_target"] == "local"


def test_cloud_worker_claims_a_cloud_job(db, admin):
    report(db, "python:cloud-1", target="cloud", provider="runpod")
    _, _, job = pending_generation_job(db, admin, p_execution_target="cloud")
    db.as_service("python")
    row = db.one("select * from claim_next_automation_job('generation', 'python:cloud-1')")
    assert row["id"] == job["id"] and row["execution_target"] == "cloud"


def test_local_worker_cannot_claim_a_cloud_job(db, admin):
    report(db, "python:local-1", target="local")
    _, _, job = pending_generation_job(db, admin, p_execution_target="cloud")

    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:local-1')") is None
    assert db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)) is None
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "pending"
    assert db.val("select attempts from automation_jobs where id = %s", (job["id"],)) == 0  # 시도 횟수도 늘지 않는다


def test_cloud_worker_cannot_claim_a_local_job(db, admin):
    report(db, "python:cloud-1", target="cloud", provider="runpod")
    _, _, job = pending_generation_job(db, admin)
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:cloud-1')") is None
    assert db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (job["id"],)) is None


def test_an_unregistered_worker_cannot_claim_a_cloud_job(db, admin):
    """워커의 Target은 요청 본문이 아니라 서버가 worker_status에서 읽는다.

    등록되지 않은 워커는 local로 본다 → cloud Job은 가져갈 수 없다. 이름을 cloud처럼 지어도 마찬가지다.
    """
    _, _, job = pending_generation_job(db, admin, p_execution_target="cloud")
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:cloud-1')") is None
    assert db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (job["id"],)) is None
    assert db.one("select * from claim_automation_job(%s, null)", (job["id"],)) is None


def test_gating_does_not_affect_other_job_types_or_running_jobs(db, admin):
    report(db, "python:local-1", target="local")
    pid = db.make_persona(admin)
    cj = to_generating(db, admin, pid)
    gen = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
    db.as_service("python")
    running = db.one("select * from claim_automation_job(%s, 'python:local-1')", (gen["id"],))

    set_default_target(db, admin, "cloud")      # 실행 중에 기본값을 바꿔도
    db.as_service("python")
    assert db.val("select heartbeat_automation_job(%s, %s)", (running["id"], running["locked_at"])) is True

    # prompt 같은 n8n Job은 Target gating과 무관하다
    cj2 = to_generating(db, admin, pid)
    pj = create_job(db, "prompt", pid, cj2["id"], key=f"prompt:{cj2['id']}:1")
    db.as_service("n8n")
    assert db.one("select * from claim_automation_job(%s, 'n8n')", (pj["id"],))["status"] == "processing"
    assert db.one("select * from claim_next_automation_job('prompt', 'n8n')") is None  # 남은 prompt Job 없음


# -----------------------------------------------------------------------------
# move_pending_jobs_to_target: 대기 중인 Job만 옮기는 명시적 관리 동작
# -----------------------------------------------------------------------------
def test_move_pending_jobs_local_to_cloud_and_back(db, admin):
    _, cj, job = pending_generation_job(db, admin)

    db.as_operator(admin)
    assert db.val("select move_pending_jobs_to_target('cloud')") == 1
    db.as_postgres()
    assert db.val("select execution_target from content_jobs where id = %s", (cj["id"],)) == "cloud"
    assert target_of(db, job["id"]) == "cloud"

    db.as_operator(admin)
    assert db.val("select move_pending_jobs_to_target('local')") == 1
    db.as_postgres()
    assert db.val("select execution_target from content_jobs where id = %s", (cj["id"],)) == "local"
    assert target_of(db, job["id"]) == "local"

    db.as_operator(admin)
    assert db.val("select move_pending_jobs_to_target('local')") == 0  # 이미 그 Target이면 0


def test_move_leaves_running_and_finished_jobs_alone(db, admin):
    report(db, "python:local-1", target="local")
    pid = db.make_persona(admin)

    cj1 = to_generating(db, admin, pid)
    running_job = create_job(db, "generation", pid, cj1["id"], key=f"generation:{cj1['id']}:1")
    db.as_service("python")
    running = db.one("select * from claim_automation_job(%s, 'python:local-1')", (running_job["id"],))
    assert running["status"] == "processing"

    cj2 = to_generating(db, admin, pid)
    pending_job = create_job(db, "generation", pid, cj2["id"], key=f"generation:{cj2['id']}:1")

    db.as_operator(admin)
    assert db.val("select move_pending_jobs_to_target('cloud')") == 1   # pending 하나만

    assert target_of(db, running_job["id"]) == "local"                  # 실행 중인 Job은 그대로
    assert target_of(db, pending_job["id"]) == "cloud"


def test_move_requires_admin_and_a_valid_target(db, admin):
    uid = db.make_operator()
    db.as_operator(uid)
    assert db.expect_error("select move_pending_jobs_to_target('cloud')").sqlstate == "PT403"

    db.as_operator(admin)
    for bad in ("mars", "", None):
        assert db.expect_error("select move_pending_jobs_to_target(%s)", (bad,)).sqlstate == "PT422", bad


def test_move_writes_an_audit_event(db, admin):
    pending_generation_job(db, admin)
    db.as_operator(admin)
    db.val("select move_pending_jobs_to_target('cloud')")
    db.as_postgres()
    event = db.one(
        "select event_type, detail from security_events where actor_id = %s order by id desc limit 1", (admin,)
    )
    assert event["event_type"] == "EXECUTION_TARGET_JOBS_MOVED"
    assert event["detail"] == {"target": "cloud", "moved": 1}


# -----------------------------------------------------------------------------
# retry_automation_job: Target 변경은 명시했을 때만 (자동 failover 없음)
# -----------------------------------------------------------------------------
def fail_job(db, job, code="MODEL_NOT_FOUND"):
    db.as_service("python")
    claimed = db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],))
    db.val(
        "select fail_automation_job(%s, %s, 'generation', %s, 'capability check failed', false)",
        (claimed["id"], claimed["locked_at"], code),
    )
    return claimed


def test_retry_keeps_the_target_when_none_is_given(db, admin):
    report(db, "python:local-1", target="local")
    _, _, job = pending_generation_job(db, admin)
    fail_job(db, job)

    db.as_operator(admin)
    assert db.one("select * from retry_automation_job(%s)", (job["id"],))["execution_target"] == "local"


def test_retry_can_move_a_failed_job_to_another_target(db, admin):
    """local에서 MODEL_NOT_FOUND → cloud로 명시 재시도 (8조)."""
    report(db, "python:local-1", target="local")
    _, cj, job = pending_generation_job(db, admin)
    fail_job(db, job)

    db.as_operator(admin)
    row = db.one("select * from retry_automation_job(%s, 'cloud')", (job["id"],))
    assert row["execution_target"] == "cloud" and row["status"] == "pending"

    db.as_postgres()
    assert db.val("select execution_target from content_jobs where id = %s", (cj["id"],)) == "cloud"  # 상위도 함께

    # 이제 local 워커는 못 가져가고 cloud 워커만 가져간다
    report(db, "python:cloud-1", target="cloud", provider="runpod")
    db.as_service("python")
    assert db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)) is None
    assert db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (job["id"],))["status"] == "processing"


def test_retry_rejects_an_invalid_target(db, admin):
    report(db, "python:local-1", target="local")
    _, _, job = pending_generation_job(db, admin)
    fail_job(db, job)

    db.as_operator(admin)
    assert db.expect_error("select * from retry_automation_job(%s, 'mars')", (job["id"],)).sqlstate == "PT422"
