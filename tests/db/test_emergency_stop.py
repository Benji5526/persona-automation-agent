"""0014 Emergency Stop, 전역 범위 (TECH_DESIGN 32.6, 33.10, CLAUDE.md 11, MVP 완료 목록 20번).

* emergency_stop_all(): admin만, 스위치 둘을 한 번에 끄고 security_events에 남긴다. 멱등.
* 막는 것: 디스패처, claim_content_job, 새 generation Job 선점 (generation_enabled).
* 계속되는 것: 이미 선점한 작업의 결과 보고, 이미 시작한 Content Job의 prompt 단계, 데이터는 그대로.
* 다시 켜기는 스위치마다 따로 (update_app_setting). Fail Closed: 값이 true가 아니거나 행이 없으면 막는다.
"""

from __future__ import annotations

import json

import pytest

from tests.db.test_db import claim, create_job, register_asset
from tests.db.test_dispatch_queued import content_status, dispatch, generation_jobs, queued

PERMISSION_DENIED = "42501"


@pytest.fixture
def admin(db):
    """관리자(소유자)와 그의 Persona."""
    uid = db.make_operator()
    pid = db.make_persona(uid)
    db.as_postgres()
    db.conn.execute("update users set role = 'admin' where id = %s", (uid,))
    return uid, pid


def stop(db, uid):
    db.as_operator(uid)
    return db.val("select emergency_stop_all()")


def switches(db):
    db.as_postgres()
    rows = db.all("select key, value from app_settings where key in ('publishing_enabled', 'generation_enabled')")
    return {r["key"]: r["value"] for r in rows}


def events(db, event_type):
    db.as_postgres()
    return db.all("select actor_type, actor_id, detail from security_events where event_type = %s order by id",
                  (event_type,))


def pending_generation(db, uid, pid):
    """queued Content Job → 디스패처 → pending generation Job."""
    cj = queued(db, uid, pid)
    assert dispatch(db) == 1
    return cj, generation_jobs(db, cj["id"])[0]


def claim_next_generation(db):
    db.as_service("python")
    return db.one("select * from claim_next_automation_job('generation', 'python:test')")


# -----------------------------------------------------------------------------
# 스위치와 RPC
# -----------------------------------------------------------------------------
def test_a_fresh_install_has_generation_on_and_publishing_off(db):
    assert switches(db) == {"generation_enabled": True, "publishing_enabled": False}


def test_emergency_stop_turns_both_switches_off_and_records_who(db, admin):
    uid, _ = admin
    db.as_postgres()
    db.conn.execute("update app_settings set value = 'true'::jsonb where key = 'publishing_enabled'")  # 게시도 켜 둔 상태

    assert stop(db, uid) == {"publishing_enabled": False, "generation_enabled": False}
    assert switches(db) == {"generation_enabled": False, "publishing_enabled": False}

    [event] = events(db, "EMERGENCY_STOP_ALL")
    assert event["actor_type"] == "operator" and event["actor_id"] == uid
    assert event["detail"]["before"] == {"generation_enabled": True, "publishing_enabled": True}   # 누가·언제·직전 값


def test_emergency_stop_is_idempotent(db, admin):
    uid, _ = admin
    first = stop(db, uid)
    assert stop(db, uid) == first
    assert switches(db) == {"generation_enabled": False, "publishing_enabled": False}
    assert len(events(db, "EMERGENCY_STOP_ALL")) == 2                   # 두 번 눌렀다는 기록은 남는다


def test_only_an_admin_can_stop_everything(db, admin):
    other = db.make_operator()
    db.as_operator(other)
    assert db.expect_error("select emergency_stop_all()").sqlstate == "PT403"
    assert switches(db)["generation_enabled"] is True and events(db, "EMERGENCY_STOP_ALL") == []


def test_anon_cannot_execute_emergency_stop(db, admin):
    db.as_anon()
    assert db.expect_error("select emergency_stop_all()").sqlstate == PERMISSION_DENIED
    assert switches(db)["generation_enabled"] is True


# -----------------------------------------------------------------------------
# 정지 중에 막히는 것
# -----------------------------------------------------------------------------
def test_the_dispatcher_starts_nothing_while_stopped(db, admin):
    uid, pid = admin
    cj = queued(db, uid, pid)
    stop(db, uid)
    assert dispatch(db) == 0
    assert content_status(db, cj["id"]) == "queued"
    assert generation_jobs(db, cj["id"]) == []


def test_claim_content_job_returns_nothing_while_stopped(db, admin):
    uid, pid = admin
    cj = queued(db, uid, pid)
    stop(db, uid)
    db.as_service("n8n")                                                # n8n 경로(WF-001)도 같은 함수를 쓴다
    assert db.one("select * from claim_content_job(%s)", (cj["id"],)) is None
    assert content_status(db, cj["id"]) == "queued"


def test_a_worker_cannot_claim_a_new_generation_job_while_stopped(db, admin):
    uid, pid = admin
    _, job = pending_generation(db, uid, pid)
    stop(db, uid)

    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:test')") is None
    assert claim(db, job["id"], "python:test") is None                  # id를 알고 있어도 선점하지 못한다
    db.as_postgres()
    row = db.one("select status, attempts, claimed_by from automation_jobs where id = %s", (job["id"],))
    assert row == {"status": "pending", "attempts": 0, "claimed_by": None}


def test_new_orders_can_still_be_queued_but_nothing_starts(db, admin):
    uid, pid = admin
    stop(db, uid)
    cj = queued(db, uid, pid)                                           # 주문은 받는다 (켜면 처리된다)
    assert cj["status"] == "queued"
    assert dispatch(db) == 0


# -----------------------------------------------------------------------------
# 정지 중에도 계속되는 것
# -----------------------------------------------------------------------------
def test_a_job_already_claimed_finishes_normally_while_stopped(db, admin):
    uid, pid = admin
    cj, job = pending_generation(db, uid, pid)
    claimed = claim_next_generation(db)
    assert claimed["id"] == job["id"] and claimed["status"] == "processing"

    stop(db, uid)                                                       # 생성 도중에 정지

    db.as_service("python")
    assert db.val("select heartbeat_automation_job(%s, %s)", (claimed["id"], claimed["locked_at"])) is not None
    asset = register_asset(db, claimed, pid)
    assert db.val("select complete_automation_job(%s, %s, %s::jsonb)",
                  (claimed["id"], claimed["locked_at"], json.dumps({"asset_ids": [str(asset["id"])]}))) is True
    assert content_status(db, cj["id"]) == "ready"                      # 끊지 않고 끝낸다


def test_stopping_does_not_touch_running_jobs_or_delete_anything(db, admin):
    uid, pid = admin
    cj, job = pending_generation(db, uid, pid)
    claim_next_generation(db)
    db.as_postgres()
    before = db.one("select (select count(*) from content_jobs) cj, (select count(*) from automation_jobs) aj,"
                    " (select count(*) from assets) a, (select count(*) from personas) p")

    stop(db, uid)

    db.as_postgres()
    after = db.one("select (select count(*) from content_jobs) cj, (select count(*) from automation_jobs) aj,"
                   " (select count(*) from assets) a, (select count(*) from personas) p")
    assert after == before                                              # 지우지 않는다
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "processing"
    assert content_status(db, cj["id"]) == "generating"


def test_a_content_job_that_already_started_keeps_its_prompt_step(db, admin):
    uid, pid = admin
    cj = queued(db, uid, pid)
    db.as_service("n8n")
    assert db.one("select * from claim_content_job(%s)", (cj["id"],))["status"] == "generating"
    prompt_job = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:1")

    stop(db, uid)

    db.as_service("n8n")
    assert claim(db, prompt_job["id"], "n8n")["status"] == "processing"  # 이미 시작한 주문의 단계는 이어진다


# -----------------------------------------------------------------------------
# 다시 켜기: 스위치마다 따로
# -----------------------------------------------------------------------------
def test_switches_are_turned_back_on_one_at_a_time(db, admin):
    uid, pid = admin
    cj = queued(db, uid, pid)
    stop(db, uid)
    assert dispatch(db) == 0

    db.as_operator(uid)
    after = db.val("select update_app_setting('generation_enabled', 'true'::jsonb)")
    assert after["generation_enabled"] is True
    assert after["publishing_enabled"] is False                         # 게시는 따로 켜기 전까지 꺼진 채다
    assert switches(db)["publishing_enabled"] is False
    assert [e["actor_id"] for e in events(db, "GENERATION_ENABLED")] == [uid]

    assert dispatch(db) == 1                                            # 생성만 다시 시작된다
    assert content_status(db, cj["id"]) == "generating"
    claimed = claim_next_generation(db)
    assert claimed is not None and claimed["status"] == "processing"


def test_turning_generation_off_alone_is_logged_and_blocks_the_dispatcher(db, admin):
    uid, pid = admin
    cj = queued(db, uid, pid)
    db.as_operator(uid)
    db.val("select update_app_setting('generation_enabled', 'false'::jsonb)")
    assert [e["actor_id"] for e in events(db, "GENERATION_DISABLED")] == [uid]
    assert dispatch(db) == 0 and content_status(db, cj["id"]) == "queued"


def test_there_is_no_resume_everything_function(db):
    db.as_postgres()
    assert db.val("select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
                  " where n.nspname = 'public' and p.proname in ('resume_all', 'emergency_resume_all',"
                  " 'emergency_start_all', 'emergency_stop_clear')") == 0


def test_generation_enabled_needs_a_boolean_and_an_admin(db, admin):
    uid, _ = admin
    db.as_operator(uid)
    assert db.expect_error("select update_app_setting('generation_enabled', '\"yes\"'::jsonb)").sqlstate == "PT422"
    assert db.expect_error("select update_app_setting('generation_enabled', '1'::jsonb)").sqlstate == "PT422"
    assert db.val("select get_app_settings()")["generation_enabled"] is True

    other = db.make_operator()
    db.as_operator(other)
    assert db.expect_error("select update_app_setting('generation_enabled', 'false'::jsonb)").sqlstate == "PT403"
    assert switches(db)["generation_enabled"] is True


# -----------------------------------------------------------------------------
# Fail Closed
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("value", ['"yes"', "1", "null", "[]"])
def test_a_value_that_is_not_true_blocks_new_generation(db, admin, value):
    uid, pid = admin
    _, job = pending_generation(db, uid, pid)
    db.as_postgres()
    db.conn.execute("update app_settings set value = %s::jsonb where key = 'generation_enabled'", (value,))
    assert claim_next_generation(db) is None
    assert claim(db, job["id"], "python:test") is None


def test_a_missing_switch_row_blocks_new_generation_and_can_be_turned_back_on(db, admin):
    uid, pid = admin
    _, job = pending_generation(db, uid, pid)
    db.as_postgres()
    db.conn.execute("delete from app_settings where key = 'generation_enabled'")
    assert claim_next_generation(db) is None

    db.as_operator(uid)
    db.val("select update_app_setting('generation_enabled', 'true'::jsonb)")   # 행이 없어도 다시 켤 수 있다
    assert claim_next_generation(db)["id"] == job["id"]
