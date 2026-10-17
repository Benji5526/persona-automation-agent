"""Worker 생애주기 감사 (최종 게이트 5번): PENDING → CLAIMED → RUNNING → SUCCEEDED / FAILED·RETRY_WAIT.

이 시스템의 상태 이름: pending → processing(= CLAIMED·RUNNING) → done | failed, 재시도 대기는 pending + 미래의 run_after.

검증하는 것
  원자적 선점 · FOR UPDATE SKIP LOCKED · 이중 선점 없음 · 크래시/Heartbeat 끊김 회수 · 폐기된 워커의 Job 회수 ·
  재시도해도 Target 불변 · 기본 Target을 바꿔도 불변 · 명시적 Target 변경 재시도 ·
  재시도 가능/불가능 구분(DB가 그 플래그를 따른다)
"""

from __future__ import annotations

import json
import threading

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.conftest import Db
from tests.db.test_db import create_job, to_generating
from tests.db.test_worker_auth import admin, claims, generation_job, issue  # noqa: F401

FORBIDDEN = "PT403"


# -----------------------------------------------------------------------------
# 동시성: 실제 커밋된 DB에서 여러 연결이 pa_worker로 동시에 선점한다
# -----------------------------------------------------------------------------
def committed_pipeline(url: str, jobs: int, workers: int, target: str = "local"):
    """커밋된 admin·Persona·generation Job 여러 개와 워커 자격 여러 개."""
    with psycopg.connect(url) as conn:
        db = Db(conn)
        uid = db.make_operator()
        db.as_postgres()
        conn.execute("update users set role = 'admin' where id = %s", (uid,))
        pid = db.make_persona(uid)
        job_ids = []
        for _ in range(jobs):
            cj = to_generating(db, uid, pid, p_execution_target=target)
            db.as_service("n8n")
            job_ids.append(str(create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")["id"]))
        creds = [(issue(db, uid, worker_id=f"python:{target}-{i}", target=target), f"python:{target}-{i}")
                 for i in range(workers)]
        conn.commit()
    return pid, job_ids, creds, target


def run_as_workers(url: str, statements: list[tuple[str, tuple]], creds, target: str) -> list[list]:
    """연결마다 pa_worker로 같은 시각에 SQL 하나를 실행하고 결과 행을 모은다."""
    barrier = threading.Barrier(len(statements))
    results: list[list] = [[] for _ in statements]
    errors: list[BaseException] = []

    def run(i: int, sql: str, params: tuple) -> None:
        jti, worker_id = creds[i]
        try:
            with psycopg.connect(url, row_factory=dict_row) as conn:
                conn.execute("set local role pa_worker")
                conn.execute("select set_config('request.jwt.claims', %s, true)",
                             (json.dumps({"role": "pa_worker", **claims(jti, worker_id=worker_id, target=target)}),))
                barrier.wait(timeout=10)
                results[i] = conn.execute(sql, params).fetchall()
                conn.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, sql, params)) for i, (sql, params) in enumerate(statements)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a concurrent worker is still running (hung)"
    assert not errors, errors
    return results


def test_concurrent_claim_of_one_job_has_exactly_one_winner(fresh_database_url):
    _, job_ids, creds, target = committed_pipeline(fresh_database_url, jobs=1, workers=4)
    stmts = [("select * from claim_automation_job(%s, 'ignored')", (job_ids[0],))] * 4
    results = run_as_workers(fresh_database_url, stmts, creds, target)

    winners = [(i, r[0]) for i, r in enumerate(results) if r]
    assert len(winners) == 1
    i, row = winners[0]
    assert row["status"] == "processing" and row["attempts"] == 1          # 한 번만 증가
    assert row["claimed_by"] == creds[i][1]                                # 클레임의 신원이 기록됐다


def test_claim_next_never_hands_one_job_to_two_workers_and_never_blocks(fresh_database_url):
    """FOR UPDATE SKIP LOCKED: Job 2개, 워커 4명 → 서로 다른 Job을 하나씩 받고 나머지는 빈 결과 (대기·중복 없음)."""
    _, job_ids, creds, target = committed_pipeline(fresh_database_url, jobs=2, workers=4)
    stmts = [("select * from claim_next_automation_job('generation', 'ignored')", ())] * 4
    results = run_as_workers(fresh_database_url, stmts, creds, target)

    got = [r[0]["id"] for r in results if r]
    assert len(got) == 2 and len(set(map(str, got))) == 2 and set(map(str, got)) == set(job_ids)
    with psycopg.connect(fresh_database_url, row_factory=dict_row) as conn:
        rows = conn.execute("select attempts, status from automation_jobs").fetchall()
    assert all(r["attempts"] == 1 and r["status"] == "processing" for r in rows)


def test_claim_next_uses_skip_locked(db):
    """구조 확인: 선점 질의가 FOR UPDATE SKIP LOCKED를 쓴다 (이 문구가 사라지면 동시 선점이 직렬화된다)."""
    db.as_postgres()
    src = db.val("select pg_get_functiondef('public.claim_next_automation_job(text, text)'::regprocedure)")
    assert "for update skip locked" in src.lower()


# -----------------------------------------------------------------------------
# 크래시·Heartbeat 끊김 회수
# -----------------------------------------------------------------------------
def go_stale(db, job_id):
    db.as_postgres()
    db.conn.execute("update automation_jobs set heartbeat_at = now() - interval '1 hour' where id = %s", (job_id,))


def run_now(db, job_id):
    db.as_postgres()
    db.conn.execute("update automation_jobs set run_after = now() where id = %s", (job_id,))


def test_a_crashed_worker_job_is_recovered_with_its_target_and_the_old_lock_is_dead(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    jti_a = issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")
    jti_b = issue(db, admin, worker_id="python:cloud-2", target="cloud", provider="runpod")

    db.as_worker(**claims(jti_a, worker_id="python:cloud-1", target="cloud"))
    first = db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))
    assert first["attempts"] == 1

    go_stale(db, job["id"])                                          # 워커가 죽었다: Heartbeat가 끊겼다
    db.as_service("python")
    assert db.val("select recover_stale_jobs()") == 1
    db.as_postgres()
    row = db.one("select status, execution_target, attempts, error_code from automation_jobs where id = %s", (job["id"],))
    assert row == {"status": "pending", "execution_target": "cloud", "attempts": 1, "error_code": "HEARTBEAT_TIMEOUT"}

    # 되살아난 옛 워커는 낡은 locked_at으로 아무것도 못 한다
    db.as_worker(**claims(jti_a, worker_id="python:cloud-1", target="cloud"))
    assert db.val("select heartbeat_automation_job(%s, %s)", (job["id"], first["locked_at"])) is False
    db.as_worker(**claims(jti_a, worker_id="python:cloud-1", target="cloud"))
    assert db.val("select complete_automation_job(%s, %s)", (job["id"], first["locked_at"])) is False

    # 백오프가 지나면 같은 Target의 다른 워커가 가져간다. Target은 처음 그대로다.
    run_now(db, job["id"])
    db.as_worker(**claims(jti_b, worker_id="python:cloud-2", target="cloud"))
    second = db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))
    assert second["claimed_by"] == "python:cloud-2" and second["attempts"] == 2
    assert second["execution_target"] == "cloud"


def test_a_job_held_by_a_revoked_worker_is_recovered_and_finished_by_another(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti_a = issue(db, admin, worker_id="python:local-1", target="local")
    jti_b = issue(db, admin, worker_id="python:local-2", target="local")
    db.as_worker(**claims(jti_a))
    held = db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))

    db.as_operator(admin)
    db.one("select * from revoke_worker_credential(%s)", (jti_a,))

    db.as_worker(**claims(jti_a))                                    # 폐기된 워커는 Heartbeat도 못 한다
    assert db.expect_error("select heartbeat_automation_job(%s, %s)", (job["id"], held["locked_at"])).sqlstate == FORBIDDEN

    go_stale(db, job["id"])
    db.as_service("python")
    assert db.val("select recover_stale_jobs()") == 1
    run_now(db, job["id"])
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))["claimed_by"] == "python:local-2"


def test_attempts_are_exhausted_by_repeated_crashes_and_the_job_fails_for_good(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin, worker_id="python:local-1", target="local")
    for _ in range(3):                                               # max_attempts 기본 3
        run_now(db, job["id"])
        db.as_worker(**claims(jti))
        assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],)) is not None
        go_stale(db, job["id"])
        db.as_service("python")
        db.val("select recover_stale_jobs()")
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "failed"


# -----------------------------------------------------------------------------
# 재시도 가능/불가능: DB가 워커가 보낸 retryable 플래그를 그대로 따른다
# -----------------------------------------------------------------------------
def fail_as_worker(db, jti, job, *, code, retryable, worker="python:local-1", target="local"):
    db.as_worker(**claims(jti, worker_id=worker, target=target))
    claimed = db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))
    db.as_worker(**claims(jti, worker_id=worker, target=target))
    return claimed, db.val("select fail_automation_job(%s, %s, 'validation', %s, 'x', %s)",
                           (job["id"], claimed["locked_at"], code, retryable))


@pytest.mark.parametrize("code", ["WORKFLOW_UNSUPPORTED_ON_TARGET", "MODEL_NOT_FOUND", "LORA_NOT_FOUND"])
def test_capability_failures_are_final_and_do_not_retry(db, admin, code):
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin)
    _, result = fail_as_worker(db, jti, job, code=code, retryable=False)
    assert result["applied"] is True and result["status"] == "failed"
    db.as_postgres()
    row = db.one("select status, attempts, error_code from automation_jobs where id = %s", (job["id"],))
    assert row == {"status": "failed", "attempts": 1, "error_code": code}


def test_a_temporary_execution_error_waits_for_retry_and_keeps_its_target(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin)
    _, result = fail_as_worker(db, jti, job, code="OUT_OF_MEMORY", retryable=True)
    assert result["status"] == "pending" and result["run_after"] is not None      # RETRY_WAIT
    db.as_postgres()
    assert db.val("select execution_target from automation_jobs where id = %s", (job["id"],)) == "local"


def test_retry_wait_is_not_claimable_until_its_time_and_only_by_the_same_target(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti_local = issue(db, admin, worker_id="python:local-1", target="local")
    jti_cloud = issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")
    fail_as_worker(db, jti_local, job, code="OUT_OF_MEMORY", retryable=True)

    db.as_worker(**claims(jti_local))
    assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],)) is None     # 백오프 중

    run_now(db, job["id"])
    db.as_worker(**claims(jti_cloud, worker_id="python:cloud-1", target="cloud", provider="runpod"))
    assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],)) is None     # 자동 failover 없음
    db.as_worker(**claims(jti_local))
    assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))["attempts"] == 2


def test_changing_the_default_target_does_not_move_a_retrying_job(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin)
    fail_as_worker(db, jti, job, code="OUT_OF_MEMORY", retryable=True)

    db.as_operator(admin)
    db.val("select update_app_setting('default_execution_target', '\"cloud\"'::jsonb)")
    db.as_postgres()
    assert db.val("select execution_target from automation_jobs where id = %s", (job["id"],)) == "local"


def test_explicit_retry_on_another_target_moves_it_and_the_old_worker_loses_it(db, admin):
    """local에서 MODEL_NOT_FOUND → 사람이 cloud로 명시 재시도 → cloud 워커만 가져간다."""
    _, cj, job = generation_job(db, admin, target="local")
    jti_local = issue(db, admin, worker_id="python:local-1", target="local")
    jti_cloud = issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")
    fail_as_worker(db, jti_local, job, code="MODEL_NOT_FOUND", retryable=False)

    db.as_operator(admin)
    assert db.one("select * from retry_automation_job(%s, 'cloud')", (job["id"],))["execution_target"] == "cloud"

    db.as_worker(**claims(jti_local))
    assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],)) is None
    db.as_worker(**claims(jti_cloud, worker_id="python:cloud-1", target="cloud", provider="runpod"))
    got = db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))
    assert got["claimed_by"] == "python:cloud-1" and got["execution_target"] == "cloud"


def test_a_failure_report_from_a_non_owner_does_not_change_the_job(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti_a = issue(db, admin, worker_id="python:local-1", target="local")
    jti_b = issue(db, admin, worker_id="python:local-2", target="local")
    db.as_worker(**claims(jti_a))
    held = db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    res = db.val("select fail_automation_job(%s, %s, 'unknown', 'X', 'sabotage', false)", (job["id"], held["locked_at"]))
    assert res["applied"] is False
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "processing"
