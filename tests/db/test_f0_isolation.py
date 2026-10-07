"""F0 보강 테스트 (TECH_DESIGN 44.5 0번).

* 45.6 Foundation 보강: 테이블 API로 직접 하는 수정·삭제, 위조 user_id, 남의 Persona Asset·Storage
* 46.7 Content Job 보강: 전이 기록, 재시도·재생성, 종료 상태
* 36.12 격리 테스트 MVP 행 + 47.3 4번 (0009 persona_isolation)
* 49.5 3번: 동시 선점 (연결 둘이 같은 시각에)
* 50.5 1번, 51.5 3번: 교차 계정 Asset·Post·Social Account 접근
"""

from __future__ import annotations

import json
import threading
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.conftest import Db
from tests.db.test_db import claim, create_job, register_asset, to_generating, to_ready

PERMISSION_DENIED = "42501"


@pytest.fixture
def two_operators(db):
    """A(Persona 있음)와 B(Persona 있음)."""
    uid_a = db.make_operator()
    pid_a = db.make_persona(uid_a, slug="gina")
    uid_b = db.make_operator()
    pid_b = db.make_persona(uid_b, slug="mina")
    return uid_a, pid_a, uid_b, pid_b


def rowcount(db, sql, params=None) -> int:
    return db.conn.execute(sql, params).rowcount


# -----------------------------------------------------------------------------
# 45.6 Foundation 보강
# -----------------------------------------------------------------------------
def test_other_operator_cannot_update_or_delete_persona(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    db.as_operator(uid_b)
    assert rowcount(db, "update personas set name = 'hijacked' where id = %s", (pid_a,)) == 0
    assert rowcount(db, "delete from personas where id = %s", (pid_a,)) == 0
    db.as_operator(uid_a)
    assert db.val("select name from personas where id = %s", (pid_a,)) == "Gina"


def test_other_operator_cannot_update_or_delete_persona_assets(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    db.as_operator(uid_a)
    asset_id = db.val("insert into persona_assets (persona_id, asset_type, name) values (%s, 'lora', 'gina.safetensors')"
                      " returning id", (pid_a,))
    db.as_operator(uid_b)
    assert rowcount(db, "update persona_assets set name = 'x' where id = %s", (asset_id,)) == 0
    assert rowcount(db, "delete from persona_assets where id = %s", (asset_id,)) == 0


def test_user_id_cannot_be_forged_on_persona(db, two_operators):
    uid_a, _, uid_b, pid_b = two_operators
    db.as_operator(uid_b)
    err = db.expect_error("insert into personas (user_id, name, slug) values (%s, 'Fake', 'fake')", (uid_a,))
    assert err.sqlstate == PERMISSION_DENIED
    err = db.expect_error("update personas set user_id = %s where id = %s", (uid_a, pid_b))
    assert err.sqlstate == PERMISSION_DENIED


def test_persona_asset_cannot_be_added_to_another_operators_persona(db, two_operators):
    _, pid_a, uid_b, _ = two_operators
    db.as_operator(uid_b)
    err = db.expect_error("insert into persona_assets (persona_id, asset_type, name) values (%s, 'lora', 'x')", (pid_a,))
    assert err.sqlstate == PERMISSION_DENIED


def test_other_operator_cannot_touch_private_storage_objects(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    path = f"persona/{pid_a}/refs/face.png"
    db.as_postgres()
    db.conn.execute("insert into storage.objects (bucket_id, name) values ('persona-private', %s)", (path,))
    db.as_operator(uid_b)
    assert rowcount(db, "update storage.objects set name = name || 'x' where bucket_id = 'persona-private'"
                        " and name = %s", (path,)) == 0
    assert rowcount(db, "delete from storage.objects where bucket_id = 'persona-private' and name = %s", (path,)) == 0
    assert db.val("select count(*) from storage.objects where bucket_id = 'persona-private' and name = %s", (path,)) == 0


# -----------------------------------------------------------------------------
# 46.7 Content Job 보강
# -----------------------------------------------------------------------------
def transitions(db, entity_id):
    db.as_postgres()
    return db.all("select from_status, to_status, reason from state_transitions where entity_id = %s order by id",
                  (entity_id,))


def test_submit_records_operator_submit_transition(db, two_operators):
    uid_a, pid_a, *_ = two_operators
    db.as_operator(uid_a)
    cj = db.one("select * from create_content_job(p_persona_id => %s, p_content_type => 'image', p_topic => 'x', p_submit => false)", (pid_a,))
    assert cj["status"] == "draft"
    db.as_operator(uid_a)
    assert db.one("select * from submit_content_job(%s)", (cj["id"],))["status"] == "queued"
    assert {"from_status": "draft", "to_status": "queued", "reason": "operator:submit"} in transitions(db, cj["id"])


def test_direct_content_job_insert_for_another_persona_is_denied(db, two_operators):
    _, pid_a, uid_b, _ = two_operators
    db.as_operator(uid_b)
    err = db.expect_error("insert into content_jobs (persona_id, content_type, topic) values (%s, 'image', 'x')", (pid_a,))
    assert err.sqlstate == PERMISSION_DENIED


def fail_content_job(db, uid, pid):
    """queued → generating → prompt Job 최종 실패 → Content Job failed."""
    cj = to_generating(db, uid, pid)
    pj = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:1", max_attempts=1)
    p = claim(db, pj["id"], "n8n")
    db.val("select fail_automation_job(%s, %s, 'validation', 'PROMPT_MISSING', 'no prompt', false)",
           (p["id"], p["locked_at"]))
    db.as_postgres()
    assert db.val("select status from content_jobs where id = %s", (cj["id"],)) == "failed"
    return cj, p


def test_retry_content_job_requeues_failed_with_new_run(db, two_operators):
    uid_a, pid_a, *_ = two_operators
    cj, _ = fail_content_job(db, uid_a, pid_a)
    db.as_operator(uid_a)
    row = db.one("select * from retry_content_job(%s)", (cj["id"],))
    assert (row["status"], row["run_number"]) == ("queued", cj["run_number"] + 1)


def test_retry_content_job_rejects_non_failed(db, two_operators):
    uid_a, pid_a, *_ = two_operators
    cj = db.create_content_job(uid_a, pid_a, p_submit=True)  # queued
    db.as_operator(uid_a)
    assert db.expect_error("select * from retry_content_job(%s)", (cj["id"],)).sqlstate == "PT409"


def test_regenerate_requeues_ready_and_respects_hourly_limit(db, two_operators):
    uid_a, pid_a, *_ = two_operators
    cj, _, _ = to_ready(db, uid_a, pid_a)
    db.as_operator(uid_a)
    row = db.one("select * from regenerate_content_job(%s)", (cj["id"],))
    assert (row["status"], row["run_number"]) == ("queued", cj["run_number"] + 1)

    cj2, _, _ = to_ready(db, uid_a, pid_a)
    db.as_postgres()
    db.conn.execute("update app_settings set value = jsonb_set(value, '{max_content_jobs_per_hour}', '1')"
                    " where key = 'limits'")
    db.as_operator(uid_a)
    assert db.expect_error("select * from regenerate_content_job(%s)", (cj2["id"],)).sqlstate == "PT429"


def test_cancelled_content_job_cannot_be_retried_or_submitted(db, two_operators):
    uid_a, pid_a, *_ = two_operators
    cj = db.create_content_job(uid_a, pid_a, p_submit=True)
    db.as_operator(uid_a)
    db.one("select * from cancel_content_job(%s, 'test')", (cj["id"],))
    assert db.expect_error("select * from retry_content_job(%s)", (cj["id"],)).sqlstate == "PT409"
    assert db.expect_error("select * from submit_content_job(%s)", (cj["id"],)).sqlstate == "PT409"


# -----------------------------------------------------------------------------
# 36.12 격리 (0009 persona_isolation)
# -----------------------------------------------------------------------------
def test_operator_cannot_read_other_operators_pipeline_rows(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    cj, _, asset = to_ready(db, uid_a, pid_a)
    db.as_postgres()
    db.conn.execute("insert into posts (persona_id, asset_id, platform, caption) values (%s, %s, 'instagram', 'x')",
                    (pid_a, asset["id"]))
    db.as_operator(uid_b)
    for table in ("content_jobs", "automation_jobs", "assets", "posts", "state_transitions"):
        assert db.val(f"select count(*) from {table}") == 0, table


def test_automation_job_must_match_content_job_persona(db, two_operators):
    uid_a, pid_a, _, pid_b = two_operators
    cj = to_generating(db, uid_a, pid_a)
    db.as_postgres()
    err = db.expect_error("insert into automation_jobs (persona_id, content_job_id, job_type, worker)"
                          " values (%s, %s, 'generation', 'python')", (pid_b, cj["id"]))
    assert err.sqlstate == "PT422" and "different persona" in str(err)
    # RPC 경로는 기존대로 NOT_FOUND
    db.as_service("n8n")
    assert db.expect_error("select * from create_automation_job('generation', %s, %s)",
                           (pid_b, cj["id"])).sqlstate == "PT404"


def test_automation_job_must_match_post_persona(db, two_operators):
    uid_a, pid_a, _, pid_b = two_operators
    _, _, asset = to_ready(db, uid_a, pid_a)
    db.as_postgres()
    post_id = db.val("insert into posts (persona_id, asset_id, platform, caption) values (%s, %s, 'instagram', 'x')"
                     " returning id", (pid_a, asset["id"]))
    err = db.expect_error("insert into automation_jobs (persona_id, post_id, job_type, worker)"
                          " values (%s, %s, 'caption', 'n8n')", (pid_b, post_id))
    assert err.sqlstate == "PT422"


def test_post_must_match_asset_and_account_persona(db, two_operators):
    uid_a, pid_a, _, pid_b = two_operators
    _, _, asset = to_ready(db, uid_a, pid_a)
    db.as_postgres()
    err = db.expect_error("insert into posts (persona_id, asset_id, platform, caption) values (%s, %s, 'instagram', 'x')",
                          (pid_b, asset["id"]))
    assert err.sqlstate == "PT422" and "asset" in str(err)
    account_b = db.val("insert into social_accounts (persona_id, platform, account_id) values (%s, 'instagram', 'b1')"
                       " returning id", (pid_b,))
    err = db.expect_error("insert into posts (persona_id, asset_id, social_account_id, platform, caption)"
                          " values (%s, %s, %s, 'instagram', 'x')", (pid_a, asset["id"], account_b))
    assert err.sqlstate == "PT422" and "social account" in str(err)
    # 같은 Persona끼리는 통과
    assert db.val("insert into posts (persona_id, asset_id, platform, caption) values (%s, %s, 'instagram', 'x')"
                  " returning id", (pid_a, asset["id"])) is not None


def test_persona_cannot_be_switched_on_existing_rows(db, two_operators):
    uid_a, pid_a, _, pid_b = two_operators
    _, _, asset = to_ready(db, uid_a, pid_a)
    db.as_postgres()
    post_id = db.val("insert into posts (persona_id, asset_id, platform, caption) values (%s, %s, 'instagram', 'x')"
                     " returning id", (pid_a, asset["id"]))
    assert db.expect_error("update posts set persona_id = %s where id = %s", (pid_b, post_id)).sqlstate == "PT422"


# -----------------------------------------------------------------------------
# 47.3 4번: 보관한 Persona의 재실행 거부
# -----------------------------------------------------------------------------
def archive_persona(db, uid, pid):
    db.as_operator(uid)
    db.conn.execute("update personas set status = 'inactive' where id = %s", (pid,))


def test_archived_persona_blocks_retry_regenerate_and_step_retry(db, two_operators):
    uid_a, pid_a, *_ = two_operators
    failed_cj, failed_job = fail_content_job(db, uid_a, pid_a)
    ready_cj, _, _ = to_ready(db, uid_a, pid_a)
    archive_persona(db, uid_a, pid_a)

    db.as_operator(uid_a)
    for sql, arg in (("select * from retry_content_job(%s)", failed_cj["id"]),
                     ("select * from regenerate_content_job(%s)", ready_cj["id"]),
                     ("select * from retry_automation_job(%s)", failed_job["id"])):
        err = db.expect_error(sql, (arg,))
        assert err.sqlstate == "PT422" and "persona is not active" in str(err), sql


def test_archived_persona_does_not_block_worker_rpcs_for_pending_work(db, two_operators):
    """이미 대기 중인 일은 끝낸다 (멈추려면 취소). Worker RPC를 막으면 Content Job이 generating에 걸린다."""
    uid_a, pid_a, *_ = two_operators
    cj = db.create_content_job(uid_a, pid_a, p_submit=True)
    archive_persona(db, uid_a, pid_a)
    db.as_service("n8n")
    assert db.one("select * from claim_content_job(%s)", (cj["id"],))["status"] == "generating"
    assert create_job(db, "prompt", pid_a, cj["id"], key=f"prompt:{cj['id']}:1") is not None


# -----------------------------------------------------------------------------
# 50.5 1번: Asset 교차 계정 접근
# -----------------------------------------------------------------------------
def test_assets_are_isolated_between_operators(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    _, _, asset = to_ready(db, uid_a, pid_a)

    db.as_operator(uid_b)
    assert db.val("select count(*) from assets where id = %s", (asset["id"],)) == 0
    assert db.expect_error("select * from archive_asset(%s)", (asset["id"],)).sqlstate == "PT404"
    assert db.expect_error("update assets set file_name = 'x' where id = %s", (asset["id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("delete from assets where id = %s", (asset["id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("insert into assets (persona_id, content_job_id, asset_type, file_name, storage_path,"
                           " mime_type) values (%s, %s, 'image', 'x.png', 'persona/x/assets/x.png', 'image/png')",
                           (asset["persona_id"], asset["content_job_id"])).sqlstate == PERMISSION_DENIED

    db.as_operator(uid_a)
    assert db.val("select count(*) from assets where id = %s", (asset["id"],)) == 1  # 주인은 본다


def test_asset_library_index_exists(db):
    assert db.val("select count(*) from pg_indexes where tablename = 'assets'"
                  " and indexname = 'assets_persona_created_idx'") == 1


# -----------------------------------------------------------------------------
# 51.5 3번: Post·Social Account 접근
# -----------------------------------------------------------------------------
@pytest.fixture
def post_of_a(db, two_operators):
    uid_a, pid_a, uid_b, pid_b = two_operators
    _, _, asset = to_ready(db, uid_a, pid_a)
    db.as_postgres()
    post_id = db.val("insert into posts (persona_id, asset_id, platform, caption) values (%s, %s, 'instagram', 'hello')"
                     " returning id", (pid_a, asset["id"]))
    account_id = db.val("insert into social_accounts (persona_id, platform, account_id) values (%s, 'instagram', 'a1')"
                        " returning id", (pid_a,))
    return {"uid_a": uid_a, "pid_a": pid_a, "uid_b": uid_b, "post_id": post_id, "account_id": account_id,
            "asset_id": asset["id"]}


def test_posts_and_social_accounts_are_isolated(db, post_of_a):
    db.as_operator(post_of_a["uid_b"])
    assert db.val("select count(*) from posts") == 0
    assert db.val("select count(*) from social_accounts") == 0
    db.as_operator(post_of_a["uid_a"])
    assert db.val("select count(*) from posts") == 1
    assert db.val("select count(*) from social_accounts") == 1


def test_operator_cannot_write_posts_or_social_accounts_directly(db, post_of_a):
    db.as_operator(post_of_a["uid_a"])  # 주인이어도 상태·연결 칸은 직접 못 쓴다
    for sql in ("insert into posts (persona_id, asset_id, platform) values (%s, %s, 'instagram')",):
        assert db.expect_error(sql, (post_of_a["pid_a"], post_of_a["asset_id"])).sqlstate == PERMISSION_DENIED
    for column, value in (("status", "'approved'"), ("scheduled_at", "now() + interval '1 day'"),
                          ("external_post_id", "'x'"), ("asset_id", f"'{post_of_a['asset_id']}'")):
        err = db.expect_error(f"update posts set {column} = {value} where id = %s", (post_of_a["post_id"],))
        assert err.sqlstate == PERMISSION_DENIED, column
    assert db.expect_error("delete from posts where id = %s", (post_of_a["post_id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("insert into social_accounts (persona_id, platform, account_id) values (%s, 'x', 'z')",
                           (post_of_a["pid_a"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("update social_accounts set username = 'x' where id = %s",
                           (post_of_a["account_id"],)).sqlstate == PERMISSION_DENIED


def test_draft_caption_is_editable_but_scheduled_post_is_not(db, post_of_a):
    db.as_operator(post_of_a["uid_a"])
    assert rowcount(db, "update posts set caption = 'edited' where id = %s", (post_of_a["post_id"],)) == 1
    db.as_postgres()
    db.conn.execute("update posts set status = 'cancelled' where id = %s", (post_of_a["post_id"],))
    db.as_operator(post_of_a["uid_a"])
    assert rowcount(db, "update posts set caption = 'again' where id = %s", (post_of_a["post_id"],)) == 0


def test_scheduled_and_published_post_captions_are_not_editable(db, post_of_a):
    """51.5 3번: draft·rejected만 직접 수정할 수 있다. scheduled·published는 0행."""
    db.as_postgres()
    db.conn.execute("alter table posts disable trigger user")   # 전이 Guard 없이 상태만 맞춘다 (정책 시험)
    db.conn.execute("update posts set status = 'scheduled', scheduled_at = now() + interval '1 day' where id = %s",
                    (post_of_a["post_id"],))
    db.as_operator(post_of_a["uid_a"])
    assert rowcount(db, "update posts set caption = 'x' where id = %s", (post_of_a["post_id"],)) == 0
    db.as_postgres()
    db.conn.execute("update posts set status = 'published', published_at = now(), external_post_id = 'e1' where id = %s",
                    (post_of_a["post_id"],))
    db.as_operator(post_of_a["uid_a"])
    assert rowcount(db, "update posts set caption = 'y' where id = %s", (post_of_a["post_id"],)) == 0
    db.as_postgres()
    assert db.val("select caption from posts where id = %s", (post_of_a["post_id"],)) == "hello"


def test_other_operator_cannot_edit_a_draft_caption(db, post_of_a):
    db.as_operator(post_of_a["uid_b"])
    assert rowcount(db, "update posts set caption = 'hijack' where id = %s", (post_of_a["post_id"],)) == 0


def test_post_cannot_skip_approval_to_publishing(db, post_of_a):
    """draft·pending_approval에서 publishing·published로 가는 길은 service_role로도 없다 (11.8)."""
    db.as_service("n8n")
    for target in ("publishing", "published"):
        err = db.expect_error("update posts set status = %s, external_post_id = 'x' where id = %s",
                              (target, post_of_a["post_id"]))
        assert err.sqlstate == "PT409", target


# -----------------------------------------------------------------------------
# 49.5 3번: 동시 선점 (연결 둘이 같은 시각에)
# -----------------------------------------------------------------------------
def committed_setup(url: str):
    """커밋된 operator·persona와 queued Content Job."""
    with psycopg.connect(url) as conn:
        db = Db(conn)
        uid = db.make_operator()
        pid = db.make_persona(uid)
        cj = db.create_content_job(uid, pid, p_submit=True)
        conn.commit()
    return uid, pid, cj


def run_concurrently(url: str, statements: list[tuple[str, tuple]], actor: str = "n8n") -> list[list]:
    """같은 시각에 시작하는 연결마다 SQL 하나를 실행하고 결과 행을 모은다."""
    barrier = threading.Barrier(len(statements))
    results: list[list] = [[] for _ in statements]
    errors: list[BaseException] = []

    def worker(i: int, sql: str, params: tuple) -> None:
        try:
            with psycopg.connect(url, row_factory=dict_row) as conn:
                conn.execute("set local role service_role")
                conn.execute("select set_config('request.headers', %s, true)", (json.dumps({"x-actor": actor}),))
                barrier.wait(timeout=10)
                results[i] = conn.execute(sql, params).fetchall()
                conn.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, sql, params)) for i, (sql, params) in enumerate(statements)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a concurrent worker is still running (hung)"
    assert not errors, errors
    return results


def test_concurrent_content_job_claim_has_one_winner(fresh_database_url):
    _, _, cj = committed_setup(fresh_database_url)
    results = run_concurrently(fresh_database_url, [("select * from claim_content_job(%s)", (cj["id"],))] * 4)
    winners = [r for r in results if r]
    assert len(winners) == 1 and winners[0][0]["status"] == "generating"


def test_concurrent_generation_job_claim_has_one_winner(fresh_database_url):
    uid, pid, cj = committed_setup(fresh_database_url)
    with psycopg.connect(fresh_database_url) as conn:
        db = Db(conn)
        db.as_service("n8n")
        assert db.one("select * from claim_content_job(%s)", (cj["id"],)) is not None
        job = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
        conn.commit()
    claims = [("select * from claim_automation_job(%s, %s)", (job["id"], f"python:{i}")) for i in range(4)]
    results = run_concurrently(fresh_database_url, claims, actor="python")
    winners = [r for r in results if r]
    assert len(winners) == 1 and winners[0][0]["status"] == "processing"
    # claim_next도 같은 Job을 두 번 주지 않는다
    with psycopg.connect(fresh_database_url) as conn:
        db = Db(conn)
        db.as_postgres()
        cj2 = db.create_content_job(uid, pid, p_submit=True)
        db.as_service("n8n")
        db.one("select * from claim_content_job(%s)", (cj2["id"],))
        create_job(db, "generation", pid, cj2["id"], key=f"generation:{cj2['id']}:1")
        conn.commit()
    nexts = [("select * from claim_next_automation_job('generation', %s)", (f"python:{i}",)) for i in range(4)]
    results = run_concurrently(fresh_database_url, nexts, actor="python")
    assert len([r for r in results if r]) == 1
