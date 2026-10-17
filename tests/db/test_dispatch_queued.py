"""0012 Local Dispatcher (TECH_DESIGN 56.10): queued Content Job → generation Job, DB 함수 + pg_cron.

n8n 없이도 '생성'이 Worker Pull까지 이어진다. 프롬프트가 있는 주문만 다루고, 정책·한도·정지 스위치를 DB가 검사한다.
"""

from __future__ import annotations

import json

import pytest

PERMISSION_DENIED = "42501"


@pytest.fixture
def op(db):
    uid = db.make_operator()
    return uid, db.make_persona(uid)


def dispatch(db, limit=None):
    """pg_cron은 postgres로 실행한다."""
    db.as_postgres()
    if limit is None:
        return db.val("select public.dispatch_queued_content_jobs()")
    return db.val("select public.dispatch_queued_content_jobs(%s)", (limit,))


def queued(db, uid, pid, prompt="portrait photo, soft light", **kwargs):
    return db.create_content_job(uid, pid, p_prompt=prompt, **kwargs)


def content_status(db, cj_id):
    db.as_postgres()
    return db.val("select status from content_jobs where id = %s", (cj_id,))


def generation_jobs(db, cj_id):
    db.as_postgres()
    return db.all("select * from automation_jobs where content_job_id = %s order by created_at", (cj_id,))


def set_setting(db, key, value):
    db.as_postgres()
    db.conn.execute(
        "insert into app_settings (key, value) values (%s, %s::jsonb)"
        " on conflict (key) do update set value = excluded.value", (key, json.dumps(value)))


# -----------------------------------------------------------------------------
# 기본 동작
# -----------------------------------------------------------------------------
def test_a_queued_job_with_a_prompt_becomes_a_pending_generation_job(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    assert cj["status"] == "queued"

    assert dispatch(db) == 1

    assert content_status(db, cj["id"]) == "generating"
    [job] = generation_jobs(db, cj["id"])
    assert (job["job_type"], job["status"], job["worker"]) == ("generation", "pending", "python")
    assert job["idempotency_key"] == f"generation:{cj['id']}:{cj['run_number']}"
    assert job["persona_id"] == pid


def test_the_dispatch_is_recorded_and_attributed_to_the_system(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    dispatch(db)
    [job] = generation_jobs(db, cj["id"])
    log = db.one("select * from execution_logs where automation_job_id = %s and step = 'DISPATCH'", (job["id"],))
    assert (log["service"], log["status"]) == ("supabase", "succeeded")
    assert log["input_data"]["dispatcher"] == "pg_cron" and log["input_data"]["content_job_id"] == str(cj["id"])
    moved = db.one("select * from state_transitions where entity_type = 'content_job' and entity_id = %s"
                   " and to_status = 'generating'", (cj["id"],))
    assert moved["actor_type"] == "system"


def test_a_second_run_does_nothing(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    assert dispatch(db) == 1
    assert dispatch(db) == 0
    assert len(generation_jobs(db, cj["id"])) == 1


def test_prompt_parts_alone_are_enough(db, op):
    uid, pid = op
    cj = db.create_content_job(uid, pid)                      # 프롬프트 없이 만들고
    db.as_postgres()
    db.conn.execute("update content_jobs set prompt_parts = %s::jsonb where id = %s",
                    (json.dumps({"subject": "Gina"}), cj["id"]))
    assert dispatch(db) == 1
    assert content_status(db, cj["id"]) == "generating"


def test_the_pulling_worker_can_claim_what_the_dispatcher_made(db, op):
    """Dispatcher → Worker Pull: 이번 변경이 이어 붙이는 두 쪽이 실제로 맞물린다."""
    uid, pid = op
    cj = queued(db, uid, pid)
    dispatch(db)
    db.as_service("python")
    row = db.one("select * from claim_next_automation_job('generation', 'python:local-1')")
    assert row is not None and row["content_job_id"] == cj["id"] and row["status"] == "processing"


# -----------------------------------------------------------------------------
# 건드리지 않는 것
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("prompt", [None, "", "   "])
def test_a_job_without_a_prompt_is_left_for_n8n_and_the_llm(db, op, prompt):
    uid, pid = op
    cj = db.create_content_job(uid, pid, **({} if prompt is None else {"p_prompt": prompt}))
    assert dispatch(db) == 0
    assert content_status(db, cj["id"]) == "queued"
    assert generation_jobs(db, cj["id"]) == []


def test_an_inactive_persona_is_skipped(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    db.as_postgres()
    db.conn.execute("update personas set status = 'inactive' where id = %s", (pid,))
    assert dispatch(db) == 0
    assert content_status(db, cj["id"]) == "queued"


def test_a_job_scheduled_for_later_is_skipped_until_due(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    db.as_postgres()
    db.conn.execute("update content_jobs set scheduled_at = now() + interval '1 hour' where id = %s", (cj["id"],))
    assert dispatch(db) == 0
    db.conn.execute("update content_jobs set scheduled_at = now() - interval '1 minute' where id = %s", (cj["id"],))
    assert dispatch(db) == 1


def test_a_cancelled_job_is_never_dispatched(db, op):
    uid, pid = op
    cancelled = queued(db, uid, pid)
    db.as_operator(uid)
    db.val("select cancel_content_job(%s)", (cancelled["id"],))
    assert dispatch(db) == 0
    assert generation_jobs(db, cancelled["id"]) == []


# -----------------------------------------------------------------------------
# 정지 스위치 (fail closed)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("key, value", [
    ("emergency_stop_all", True),
    ("emergency_stop_all", "yes"),          # 알 수 없는 값은 닫힌 것으로 본다
    ("generation_enabled", False),
    ("generation_enabled", "maybe"),
])
def test_stop_switches_block_dispatching(db, op, key, value):
    uid, pid = op
    cj = queued(db, uid, pid)
    set_setting(db, key, value)
    assert dispatch(db) == 0
    assert content_status(db, cj["id"]) == "queued"


def test_open_switches_do_not_block(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    set_setting(db, "emergency_stop_all", False)
    set_setting(db, "generation_enabled", True)
    assert dispatch(db) == 1
    assert content_status(db, cj["id"]) == "generating"


# -----------------------------------------------------------------------------
# 순서와 한도
# -----------------------------------------------------------------------------
def test_higher_priority_goes_first_and_the_limit_is_respected(db, op):
    uid, pid = op
    low = queued(db, uid, pid, p_priority=1)
    high = queued(db, uid, pid, p_priority=9)
    assert dispatch(db, limit=1) == 1
    assert content_status(db, high["id"]) == "generating"
    assert content_status(db, low["id"]) == "queued"
    assert dispatch(db, limit=1) == 1
    assert content_status(db, low["id"]) == "generating"


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_the_limit_argument_is_validated(db, limit):
    db.as_postgres()
    assert db.expect_error("select public.dispatch_queued_content_jobs(%s)", (limit,)).sqlstate == "PT422"


def test_the_daily_generation_limit_keeps_the_job_queued_and_leaves_one_visible_error(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    db.as_postgres()
    db.conn.execute("update app_settings set value = jsonb_set(value, '{daily_generation_limit}', '0') where key = 'limits'")

    assert dispatch(db) == 0
    assert content_status(db, cj["id"]) == "queued"            # 선점도 함께 되돌아간다
    assert generation_jobs(db, cj["id"]) == []

    err = db.one("select * from system_errors where service = 'dispatcher'")
    assert (err["error_type"], err["error_code"], err["retryable"], err["persona_id"]) == ("policy", "RATE_LIMITED", True, pid)

    dispatch(db)                                                # 같은 원인은 1시간에 한 줄만
    assert db.val("select count(*) from system_errors where service = 'dispatcher'") == 1

    # 한도를 풀면 다음 주기에 흘러간다
    db.conn.execute("update app_settings set value = jsonb_set(value, '{daily_generation_limit}', '300') where key = 'limits'")
    assert dispatch(db) == 1


# -----------------------------------------------------------------------------
# Target·권한
# -----------------------------------------------------------------------------
def test_the_dispatcher_never_picks_a_target_it_inherits_it(db, op):
    uid, pid = op
    local = queued(db, uid, pid, p_execution_target="local")
    cloud = queued(db, uid, pid, p_execution_target="cloud")
    assert dispatch(db) == 2
    assert {generation_jobs(db, c["id"])[0]["execution_target"] for c in (local, cloud)} == {"local", "cloud"}
    assert generation_jobs(db, local["id"])[0]["execution_target"] == "local"
    assert generation_jobs(db, cloud["id"])[0]["execution_target"] == "cloud"


def test_only_cron_and_service_role_can_run_it(db, op):
    uid, pid = op
    queued(db, uid, pid)
    sql = "select public.dispatch_queued_content_jobs()"

    db.as_anon()
    assert db.expect_error(sql).sqlstate == PERMISSION_DENIED
    db.as_operator(uid)
    assert db.expect_error(sql).sqlstate == PERMISSION_DENIED
    db.as_worker(role="pa_worker", worker_id="python:cloud-1", target="cloud", provider="x",
                 jti="j1", exp=9999999999)
    assert db.expect_error(sql).sqlstate == PERMISSION_DENIED

    db.as_service("n8n")
    assert db.val(sql) == 1


# -----------------------------------------------------------------------------
# 고아 상태 없음, 여러 건, 이미 있는 Job, DB 오류, 재시도·재생성, 동시 실행
# -----------------------------------------------------------------------------
def orphans(db):
    """generating인데 활성 generation Job이 하나도 없는 Content Job."""
    db.as_postgres()
    return db.all(
        "select c.id from content_jobs c where c.status = 'generating' and not exists"
        " (select 1 from automation_jobs a where a.content_job_id = c.id and a.job_type = 'generation')")


def test_multiple_queued_jobs_are_all_dispatched_one_job_each(db, op):
    uid, pid = op
    jobs = [queued(db, uid, pid, prompt=f"photo {i}") for i in range(3)]
    assert dispatch(db) == 3
    for cj in jobs:
        assert content_status(db, cj["id"]) == "generating"
        assert len(generation_jobs(db, cj["id"])) == 1
    assert orphans(db) == []


def test_an_existing_job_for_the_same_run_is_reused_not_duplicated(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    db.as_postgres()
    key = f"generation:{cj['id']}:{cj['run_number']}"
    db.conn.execute(
        "insert into automation_jobs (persona_id, content_job_id, job_type, worker, idempotency_key)"
        " values (%s, %s, 'generation', 'python', %s)", (pid, cj["id"], key))
    dispatch(db)
    assert len(generation_jobs(db, cj["id"])) == 1                     # 두 번째가 생기지 않았다
    assert content_status(db, cj["id"]) == "generating"
    assert orphans(db) == []


def test_a_database_error_leaves_no_orphan_and_does_not_block_the_queue(db, op):
    uid, pid = op
    bad = queued(db, uid, pid, prompt="bad one", p_priority=9)       # 먼저 처리된다
    good = queued(db, uid, pid, prompt="good one", p_priority=1)
    db.as_postgres()
    db.conn.execute(
        "create function pg_temp.boom() returns trigger language plpgsql as"
        " $f$ begin if new.content_job_id = '%s' then raise exception 'injected failure' using errcode = 'XX001';"
        " end if; return new; end $f$" % bad["id"])
    db.conn.execute("create trigger boom before insert on automation_jobs for each row execute function pg_temp.boom()")

    assert dispatch(db) == 1                                           # 나쁜 것은 건너뛰고 좋은 것은 처리
    assert content_status(db, bad["id"]) == "queued"                   # 선점이 되돌아갔다
    assert generation_jobs(db, bad["id"]) == []
    assert content_status(db, good["id"]) == "generating"
    assert orphans(db) == []

    err = db.one("select * from system_errors where service = 'dispatcher'")
    assert (err["error_type"], err["error_code"], err["persona_id"]) == ("unknown", "XX001", pid)
    assert str(bad["id"]) in err["message"]


def test_retry_and_regenerate_flow_through_the_dispatcher_with_a_new_run_key(db, op):
    uid, pid = op
    cj = queued(db, uid, pid)
    dispatch(db)
    [first] = generation_jobs(db, cj["id"])

    # 실패 → 재시도(run 2) → 새 키로 새 generation Job
    db.as_postgres()
    db.conn.execute("update automation_jobs set status = 'processing', locked_at = now(), claimed_by = 'x'"
                    " where id = %s", (first["id"],))
    db.conn.execute("update automation_jobs set status = 'failed', error_type = 'validation',"
                    " error_code = 'X', error_message = 'x' where id = %s", (first["id"],))
    assert content_status(db, cj["id"]) == "failed"
    db.as_operator(uid)
    again = db.one("select * from retry_content_job(%s)", (cj["id"],))
    assert (again["status"], again["run_number"]) == ("queued", cj["run_number"] + 1)

    assert dispatch(db) == 1
    keys = [j["idempotency_key"] for j in generation_jobs(db, cj["id"])]
    assert keys == [f"generation:{cj['id']}:{cj['run_number']}", f"generation:{cj['id']}:{cj['run_number'] + 1}"]
    assert orphans(db) == []


# -----------------------------------------------------------------------------
# 동시 실행: 서로 다른 연결 둘 이상이 같은 시각에 돌아도 Content Job마다 generation Job은 정확히 하나
# -----------------------------------------------------------------------------
def committed_queued_jobs(url, count):
    import psycopg
    from tests.conftest import Db
    with psycopg.connect(url) as conn:
        db = Db(conn)
        uid = db.make_operator()
        pid = db.make_persona(uid)
        ids = [db.create_content_job(uid, pid, p_prompt=f"photo {i}")["id"] for i in range(count)]
        conn.commit()
    return pid, ids


@pytest.mark.parametrize("dispatchers, jobs", [(2, 1), (4, 3)])
def test_concurrent_dispatchers_create_exactly_one_job_per_content_job(fresh_database_url, dispatchers, jobs):
    import psycopg
    from tests.db.test_f0_isolation import run_concurrently

    pid, ids = committed_queued_jobs(fresh_database_url, jobs)
    results = run_concurrently(fresh_database_url,
                               [("select public.dispatch_queued_content_jobs() as n", ())] * dispatchers)

    assert sum(r[0]["n"] for r in results) == jobs                    # 어느 Content Job도 두 번 처리되지 않았다
    with psycopg.connect(fresh_database_url) as conn:
        rows = conn.execute("select content_job_id, count(*) from automation_jobs"
                            " where job_type = 'generation' group by 1").fetchall()
        assert sorted(r[0] for r in rows) == sorted(ids) and all(r[1] == 1 for r in rows)
        assert conn.execute("select count(*) from content_jobs where status = 'queued'").fetchone()[0] == 0
        assert conn.execute("select count(*) from system_errors where service = 'dispatcher'").fetchone()[0] == 0
