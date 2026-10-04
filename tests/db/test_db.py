"""MVP DB 테스트 (TECH_DESIGN 16.12 Database 항목, 11번 State Machine, 15번 Security)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

PARTS = {"subject": "Gina", "location": "Tokyo at night", "action": "taking a photo", "style": "photorealistic"}
PERMISSION_DENIED = "42501"


# -----------------------------------------------------------------------------
# 도우미: Worker 입장에서 파이프라인을 한 단계씩 진행
# -----------------------------------------------------------------------------
def claim(db, job_id, worker="python:test"):
    return db.one("select * from claim_automation_job(%s, %s)", (job_id, worker))


def create_job(db, job_type, persona_id, content_job_id, key=None, max_attempts=3):
    return db.one(
        "select * from create_automation_job(p_job_type => %s, p_persona_id => %s, p_content_job_id => %s,"
        " p_max_attempts => %s, p_idempotency_key => %s)",
        (job_type, persona_id, content_job_id, max_attempts, key),
    )


def register_asset(db, job, persona_id, asset_id=None):
    asset_id = asset_id or uuid.uuid4()
    asset = {
        "id": str(asset_id),
        "asset_type": "image",
        "file_name": f"{asset_id}.png",
        "storage_path": f"persona/{persona_id}/assets/{asset_id}.png",
        "public_url": f"https://example.supabase.co/storage/v1/object/public/media/persona/{persona_id}/assets/{asset_id}.png",
        "mime_type": "image/png",
        "width": 1024,
        "height": 1536,
        "prompt": "Gina, Tokyo at night",
        "generation_metadata": {"seed": 42, "workflow": "image_generation_v1"},
    }
    return db.one("select * from register_asset(%s, %s, %s::jsonb)", (job["id"], job["locked_at"], json.dumps(asset)))


def to_generating(db, uid, pid, **kwargs):
    cj = db.create_content_job(uid, pid, **kwargs)
    db.as_service("n8n")
    assert db.one("select * from claim_content_job(%s)", (cj["id"],))["status"] == "generating"
    return cj


def to_ready(db, uid, pid):
    """Content Job을 queued → generating → (prompt) → (generation) → ready까지 진행한다."""
    cj = to_generating(db, uid, pid)

    pj = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:1")
    p = claim(db, pj["id"], "n8n")
    assert db.val("select save_prompt_parts(%s, %s, %s::jsonb, array['blurry'])",
                  (p["id"], p["locked_at"], json.dumps(PARTS))) is True
    assert db.val("select complete_automation_job(%s, %s)", (p["id"], p["locked_at"])) is True

    gj = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
    db.as_service("python")
    g = claim(db, gj["id"])
    asset = register_asset(db, g, pid)
    assert db.val("select complete_automation_job(%s, %s, %s::jsonb)",
                  (g["id"], g["locked_at"], json.dumps({"asset_ids": [str(asset["id"])]}))) is True
    return cj, g, asset


@pytest.fixture
def op(db):
    uid = db.make_operator()
    pid = db.make_persona(uid)
    return uid, pid


# -----------------------------------------------------------------------------
# 15.3 가입 허용 목록
# -----------------------------------------------------------------------------
def test_signup_rejects_email_not_on_allow_list(db):
    db.as_postgres()
    err = db.expect_error("insert into auth.users (email) values ('stranger@example.com')")
    assert "SIGNUP_NOT_ALLOWED" in str(err)


def test_signup_creates_user_row_for_allowed_email(db):
    uid = db.make_operator("Gina.Owner@Example.com")
    row = db.one("select email, role, display_name from users where id = %s", (uid,))
    assert row == {"email": "gina.owner@example.com", "role": "operator", "display_name": "Test Operator"}


# -----------------------------------------------------------------------------
# 15.4 권한: RLS, 칸 단위 권한, 함수 권한
# -----------------------------------------------------------------------------
def test_operator_cannot_see_other_operators_data(db, op):
    uid_a, pid_a = op
    db.create_content_job(uid_a, pid_a)
    uid_b = db.make_operator()

    db.as_operator(uid_b)
    assert db.val("select count(*) from personas") == 0
    assert db.val("select count(*) from content_jobs") == 0
    err = db.expect_error("select create_content_job(%s, 'image', 'x')", (pid_a,))
    assert err.sqlstate == "PT404"


def test_operator_cannot_write_status_or_role_directly(db, op):
    uid, pid = op
    cj = db.create_content_job(uid, pid, p_submit=False)
    db.as_operator(uid)
    assert db.expect_error("update content_jobs set status = 'queued' where id = %s", (cj["id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("update users set role = 'admin' where id = %s", (uid,)).sqlstate == PERMISSION_DENIED
    db.conn.execute("update users set display_name = 'Gina Owner' where id = %s", (uid,))
    assert db.val("select display_name from users where id = %s", (uid,)) == "Gina Owner"


def test_anon_has_no_access(db, op):
    db.as_anon()
    assert db.expect_error("select * from personas").sqlstate == PERMISSION_DENIED
    assert db.expect_error("select * from app_settings").sqlstate == PERMISSION_DENIED
    assert db.expect_error("select get_dashboard_summary()").sqlstate == PERMISSION_DENIED
    assert db.expect_error("select recover_stale_jobs()").sqlstate == PERMISSION_DENIED


def test_operator_cannot_call_worker_rpc_or_read_settings(db, op):
    uid, _ = op
    db.as_operator(uid)
    assert db.expect_error("select * from claim_automation_job(gen_random_uuid(), 'x')").sqlstate == PERMISSION_DENIED
    assert db.expect_error("select recover_stale_jobs()").sqlstate == PERMISSION_DENIED
    assert db.expect_error("select * from app_settings").sqlstate == PERMISSION_DENIED


def test_draft_is_editable_only_while_draft(db, op):
    uid, pid = op
    db.as_operator(uid)
    cj_id = db.val("insert into content_jobs (persona_id, content_type, topic) values (%s, 'image', 'a') returning id", (pid,))
    db.conn.execute("update content_jobs set topic = 'b' where id = %s", (cj_id,))
    db.one("select * from submit_content_job(%s)", (cj_id,))
    cur = db.conn.execute("update content_jobs set topic = 'c' where id = %s", (cj_id,))
    assert cur.rowcount == 0  # queued가 되면 RLS가 수정을 막는다
    assert db.val("select topic from content_jobs where id = %s", (cj_id,)) == "b"


# -----------------------------------------------------------------------------
# 12.4 Operator RPC, 11.14 기록
# -----------------------------------------------------------------------------
def test_create_content_job_submits_and_records_transition(db, op):
    uid, pid = op
    cj = db.create_content_job(uid, pid)
    assert cj["status"] == "queued" and cj["created_by"] == uid and cj["source"] == "operator"
    db.as_postgres()
    log = db.one("select * from state_transitions where entity_id = %s", (cj["id"],))
    assert (log["from_status"], log["to_status"], log["actor_type"], log["actor_id"], log["reason"]) == (
        None, "queued", "operator", uid, "operator:create_and_submit")


def test_create_content_job_validates_input(db, op):
    uid, pid = op
    db.as_operator(uid)
    assert db.expect_error("select create_content_job(%s, 'image')", (pid,)).sqlstate == "PT422"  # topic·prompt 없음
    bad = json.dumps({"source_face": {"persona_asset_id": str(uuid.uuid4())}})
    assert db.expect_error("select create_content_job(%s, 'image', 'x', p_input_images => %s::jsonb)",
                           (pid, bad)).sqlstate == "PT422"
    assert db.expect_error("select create_content_job(%s, 'image', 'x', p_variants => 9)", (pid,)).sqlstate == "PT422"


def test_rate_limit_on_content_jobs(db, op):
    uid, pid = op
    db.as_postgres()
    db.conn.execute("update app_settings set value = jsonb_set(value, '{max_content_jobs_per_hour}', '2') where key = 'limits'")
    db.create_content_job(uid, pid)
    db.create_content_job(uid, pid)
    db.as_operator(uid)
    assert db.expect_error("select create_content_job(%s, 'image', 'x')", (pid,)).sqlstate == "PT429"


def test_dashboard_summary(db, op):
    uid, pid = op
    db.create_content_job(uid, pid)
    db.as_operator(uid)
    summary = db.val("select get_dashboard_summary()")
    assert summary["personas"] == 1
    assert summary["content_jobs"] == {"queued": 1}
    assert summary["automation_jobs"] == {"active": 0, "pending": 0, "retry": 0, "failed": 0}
    assert summary["publishing_enabled"] is False


# -----------------------------------------------------------------------------
# 11 State Machine: 전환 강제, 정상 흐름, Rollup
# -----------------------------------------------------------------------------
def test_disallowed_transition_is_rejected_even_for_service_role(db, op):
    uid, pid = op
    cj = db.create_content_job(uid, pid)
    db.as_service("n8n")
    err = db.expect_error("update content_jobs set status = 'published' where id = %s", (cj["id"],))
    assert err.sqlstate == "PT409" and "INVALID_TRANSITION" in str(err)


def test_happy_path_reaches_ready_with_audit_trail(db, op):
    uid, pid = op
    cj, g, asset = to_ready(db, uid, pid)
    db.as_postgres()
    row = db.one("select status, completed_at, prompt_parts, metadata from content_jobs where id = %s", (cj["id"],))
    assert row["status"] == "ready" and row["completed_at"] is not None
    assert row["prompt_parts"] == PARTS and row["metadata"]["negative_additions"] == ["blurry"]
    trail = db.all(
        "select from_status, to_status, actor_type, reason from state_transitions"
        " where entity_type = 'content_job' and entity_id = %s order by id", (cj["id"],))
    assert [(t["from_status"], t["to_status"], t["actor_type"]) for t in trail] == [
        (None, "queued", "operator"), ("queued", "generating", "n8n"), ("generating", "ready", "system")]
    assert trail[-1]["reason"] == "rollup:generation_done"
    assert db.val("select actor_type from state_transitions where entity_id = %s and to_status = 'done'", (g["id"],)) == "python"


def test_generation_cannot_complete_without_asset(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"])
    db.as_service("python")
    g = claim(db, gj["id"])
    err = db.expect_error("select complete_automation_job(%s, %s)", (g["id"], g["locked_at"]))
    assert err.sqlstate == "PT422"


def test_results_require_the_current_lock(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"])
    db.as_service("python")
    g = claim(db, gj["id"])
    stale = g["locked_at"] - timedelta(seconds=1)
    assert db.val("select heartbeat_automation_job(%s, %s)", (g["id"], stale)) is False
    assert db.val("select complete_automation_job(%s, %s)", (g["id"], stale)) is False
    assert register_asset(db, {"id": g["id"], "locked_at": stale}, pid) is None
    assert db.val("select heartbeat_automation_job(%s, %s)", (g["id"], g["locked_at"])) is True


# -----------------------------------------------------------------------------
# 14.11 재시도, 11.9 R2, 12.4 단계 재실행
# -----------------------------------------------------------------------------
def test_retryable_failure_backs_off_then_fails_and_rolls_up(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"], max_attempts=2)
    db.as_service("python")

    g = claim(db, gj["id"])
    out = db.val("select fail_automation_job(%s, %s, 'timeout', 'TIMEOUT', 'comfy timeout', true)", (g["id"], g["locked_at"]))
    assert out["applied"] is True and out["status"] == "pending"
    wait = datetime.fromisoformat(out["run_after"]) - datetime.now(timezone.utc)
    assert timedelta(seconds=25) < wait <= timedelta(seconds=31)
    assert claim(db, gj["id"]) is None  # run_after 전에는 선점 불가

    db.as_postgres()
    db.conn.execute("update automation_jobs set run_after = now() - interval '1 second' where id = %s", (gj["id"],))
    db.as_service("python")
    g = claim(db, gj["id"])
    out = db.val("select fail_automation_job(%s, %s, 'timeout', 'TIMEOUT', 'again', true)", (g["id"], g["locked_at"]))
    assert out["status"] == "failed"

    db.as_postgres()
    assert db.val("select status from content_jobs where id = %s", (cj["id"],)) == "failed"
    assert db.val("select count(*) from system_errors where automation_job_id = %s", (gj["id"],)) == 2

    # Operator가 실패한 단계만 다시 실행 → Job pending, Content Job generating
    db.as_operator(uid)
    retried = db.one("select * from retry_automation_job(%s)", (gj["id"],))
    assert retried["status"] == "pending" and retried["attempts"] == 0
    assert db.val("select status from content_jobs where id = %s", (cj["id"],)) == "generating"


def test_non_retryable_failure_fails_immediately(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"])
    db.as_service("python")
    g = claim(db, gj["id"])
    out = db.val("select fail_automation_job(%s, %s, 'validation', 'LORA_NOT_FOUND', 'missing lora', false)",
                 (g["id"], g["locked_at"]))
    assert out["status"] == "failed"


# -----------------------------------------------------------------------------
# 11.6 Heartbeat 회수
# -----------------------------------------------------------------------------
def test_recover_stale_jobs_requeues_then_fails(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"], max_attempts=2)
    db.as_service("python")
    g = claim(db, gj["id"])

    db.as_postgres()
    db.conn.execute("update automation_jobs set heartbeat_at = now() - interval '10 minutes' where id = %s", (g["id"],))
    db.as_service("system")
    assert db.val("select recover_stale_jobs()") == 1
    row = db.one("select status, error_code from automation_jobs where id = %s", (g["id"],))
    assert row == {"status": "pending", "error_code": "HEARTBEAT_TIMEOUT"}
    log = db.one("select actor_type, reason from state_transitions where entity_id = %s order by id desc limit 1", (g["id"],))
    assert log == {"actor_type": "system", "reason": "heartbeat_timeout"}
    # 늦게 살아난 Worker의 결과는 반영되지 않는다
    assert db.val("select complete_automation_job(%s, %s)", (g["id"], g["locked_at"])) is False

    db.as_postgres()
    db.conn.execute("update automation_jobs set run_after = now() - interval '1 second' where id = %s", (g["id"],))
    db.as_service("python")
    g = claim(db, gj["id"])
    db.as_postgres()
    db.conn.execute("update automation_jobs set heartbeat_at = now() - interval '10 minutes' where id = %s", (g["id"],))
    db.as_service("system")
    assert db.val("select recover_stale_jobs()") == 1
    assert db.val("select status from automation_jobs where id = %s", (g["id"],)) == "failed"
    assert db.val("select status from content_jobs where id = %s", (cj["id"],)) == "failed"


# -----------------------------------------------------------------------------
# 11.5 · 14.17 중복 방지
# -----------------------------------------------------------------------------
def test_idempotent_job_creation_and_single_claim(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid)
    first = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:1")
    again = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:1")
    other = create_job(db, "prompt", pid, cj["id"], key=f"prompt:{cj['id']}:other")
    assert first["id"] == again["id"] == other["id"]  # 같은 키 또는 같은 활성 단계 → 기존 Job

    assert claim(db, first["id"], "n8n")["status"] == "processing"
    assert claim(db, first["id"], "n8n") is None  # 두 번째 선점은 빈 결과


def test_claim_content_job_only_once(db, op):
    uid, pid = op
    cj = db.create_content_job(uid, pid)
    db.as_service("n8n")
    assert db.one("select * from claim_content_job(%s)", (cj["id"],)) is not None
    assert db.one("select * from claim_content_job(%s)", (cj["id"],)) is None


def test_jobs_cannot_be_created_for_wrong_content_job_state(db, op):
    uid, pid = op
    cj = db.create_content_job(uid, pid)  # queued (아직 선점 전)
    db.as_service("n8n")
    err = db.expect_error("select create_automation_job('generation', %s, %s)", (pid, cj["id"]))
    assert err.sqlstate == "PT409"


# -----------------------------------------------------------------------------
# 11.9 R5 취소
# -----------------------------------------------------------------------------
def test_cancel_cascades_and_discards_late_results(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"])
    db.as_service("python")
    g = claim(db, gj["id"])

    db.as_operator(uid)
    db.one("select * from cancel_content_job(%s, 'changed my mind')", (cj["id"],))
    db.as_service("python")
    assert db.val("select status from automation_jobs where id = %s", (g["id"],)) == "cancelled"
    assert register_asset(db, g, pid) is None
    assert db.val("select complete_automation_job(%s, %s)", (g["id"], g["locked_at"])) is False


# -----------------------------------------------------------------------------
# Caption 초안, Post 전환과 R4, 종료 상태
# -----------------------------------------------------------------------------
def test_caption_draft_then_post_publish_rolls_up(db, op):
    uid, pid = op
    cj, _, asset = to_ready(db, uid, pid)

    db.as_service("n8n")
    cjob = create_job(db, "caption", pid, cj["id"], key=f"caption:{asset['id']}")
    c = claim(db, cjob["id"], "n8n")
    post = db.one("select * from create_post_draft(%s, %s, %s, 'instagram', %s, array['tokyo', 'night'])",
                  (c["id"], c["locked_at"], asset["id"], "도쿄의 밤 ✨"))
    assert post["status"] == "draft"
    assert db.val("select complete_automation_job(%s, %s)", (c["id"], c["locked_at"])) is True

    # Operator는 draft 캡션을 수정할 수 있다
    db.as_operator(uid)
    db.conn.execute("update posts set caption = '도쿄의 밤' where id = %s", (post["id"],))

    # V1 흐름을 DB 수준에서 확인: 승인 경로 없이는 게시 불가, 게시 ID 없이는 published 불가
    db.as_postgres()
    assert db.expect_error("update posts set status = 'publishing' where id = %s", (post["id"],)).sqlstate == "PT409"
    acct = db.val("insert into social_accounts (persona_id, platform, account_id, status)"
                  " values (%s, 'instagram', 'ig-1', 'active') returning id", (pid,))
    db.conn.execute("update posts set social_account_id = %s, status = 'pending_approval' where id = %s", (acct, post["id"]))
    for status in ("approved", "publishing"):
        db.conn.execute("update posts set status = %s where id = %s", (status, post["id"]))
    assert db.expect_error("update posts set status = 'published' where id = %s", (post["id"],)).sqlstate == "23514"
    db.conn.execute("update posts set status = 'published', external_post_id = 'ig-post-1' where id = %s", (post["id"],))
    assert db.val("select status from content_jobs where id = %s", (cj["id"],)) == "published"  # R4
    assert db.expect_error("update posts set status = 'draft' where id = %s", (post["id"],)).sqlstate == "PT409"


def test_archived_asset_is_terminal(db, op):
    uid, pid = op
    _, _, asset = to_ready(db, uid, pid)
    db.as_operator(uid)
    assert db.one("select * from archive_asset(%s)", (asset["id"],))["status"] == "archived"
    db.as_postgres()
    assert db.expect_error("update assets set status = 'approved' where id = %s", (asset["id"],)).sqlstate == "PT409"


def test_register_asset_rejects_foreign_storage_path(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"])
    db.as_service("python")
    g = claim(db, gj["id"])
    other = uuid.uuid4()
    asset = {"asset_type": "image", "file_name": "x.png", "storage_path": f"persona/{other}/assets/x.png", "mime_type": "image/png"}
    err = db.expect_error("select register_asset(%s, %s, %s::jsonb)", (g["id"], g["locked_at"], json.dumps(asset)))
    assert err.sqlstate == "PT422"


# -----------------------------------------------------------------------------
# 12.5 Registry 동기화, 15.21 로그 가리기, 15.5 Storage
# -----------------------------------------------------------------------------
def test_workflow_registry_sync(db, op):
    uid, _ = op
    db.as_service("python")
    workflows = {
        "image_generation_v1": {"version": "1.0", "type": "image", "stage": "mvp", "params": {"steps": {"default": 30}}},
        "faceswap_v1": {"version": "1.0", "type": "image", "stage": "mvp"},
    }
    assert db.val("select sync_workflow_registry(%s::jsonb)", (json.dumps(workflows),)) == 2
    del workflows["faceswap_v1"]
    db.val("select sync_workflow_registry(%s::jsonb)", (json.dumps(workflows),))
    db.as_operator(uid)
    rows = {r["id"]: r["enabled"] for r in db.all("select id, enabled from comfy_workflows")}
    assert rows == {"image_generation_v1": True, "faceswap_v1": False}


def test_execution_log_redacts_secrets(db, op):
    uid, pid = op
    cj = to_generating(db, uid, pid)
    job = create_job(db, "prompt", pid, cj["id"])
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    db.val("select log_execution(%s, 'LLM_PROMPT', 'llm', 'failed', %s::jsonb, null, 12, 'exec-1', %s)",
           (job["id"], json.dumps({"Authorization": "Bearer abc.def", "nested": {"api_key": "sk-123"}, "note": jwt}),
            f"upstream said {jwt}"))
    db.as_postgres()
    row = db.one("select input_data, error from execution_logs where automation_job_id = %s", (job["id"],))
    text = json.dumps(row["input_data"]) + row["error"]
    assert "abc.def" not in text and "sk-123" not in text and jwt not in text
    assert row["input_data"]["Authorization"] == "[REDACTED]"


def test_storage_private_bucket_is_owner_only(db, op):
    uid_a, pid_a = op
    uid_b = db.make_operator()
    pid_b = db.make_persona(uid_b, slug="mina")
    db.as_operator(uid_a)
    db.conn.execute("insert into storage.objects (bucket_id, name) values ('persona-private', %s)",
                    (f"persona/{pid_a}/refs/face.png",))
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('persona-private', %s)",
                           (f"persona/{pid_b}/refs/face.png",)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)",
                           (f"persona/{pid_a}/assets/x.png",)).sqlstate == PERMISSION_DENIED
    db.as_operator(uid_b)
    assert db.val("select count(*) from storage.objects where bucket_id = 'persona-private'") == 0


# -----------------------------------------------------------------------------
# 보안 리뷰 회귀 테스트 (M1 리뷰 지적 사항)
# -----------------------------------------------------------------------------
def test_direct_draft_insert_cannot_reference_other_personas_assets(db, op):
    """HIGH 1: RPC를 거치지 않는 직접 INSERT·UPDATE도 input_images를 검증한다."""
    uid_a, pid_a = op
    uid_b = db.make_operator()
    pid_b = db.make_persona(uid_b, slug="mina")
    db.as_operator(uid_b)
    face_b = db.val("insert into persona_assets (persona_id, asset_type, name, storage_path)"
                    " values (%s, 'face_ref', 'mina.png', %s) returning id", (pid_b, f"persona/{pid_b}/refs/mina.png"))

    db.as_operator(uid_a)
    foreign = json.dumps({"source_face": {"persona_asset_id": str(face_b)}})
    err = db.expect_error("insert into content_jobs (persona_id, content_type, topic, input_images)"
                          " values (%s, 'image', 'x', %s::jsonb)", (pid_a, foreign))
    assert err.sqlstate == "PT422"
    cj_id = db.val("insert into content_jobs (persona_id, content_type, topic) values (%s, 'image', 'x') returning id", (pid_a,))
    err = db.expect_error("update content_jobs set input_images = %s::jsonb where id = %s", (foreign, cj_id))
    assert err.sqlstate == "PT422"


def test_persona_asset_path_must_stay_in_own_refs_folder(db, op):
    """HIGH 1: persona_assets.storage_path로 다른 Persona의 파일을 가리킬 수 없다."""
    uid_a, pid_a = op
    other = uuid.uuid4()
    db.as_operator(uid_a)
    for path in (f"persona/{other}/refs/x.png", f"persona/{pid_a}/assets/x.png", f"persona/{pid_a}/refs/../../x.png"):
        err = db.expect_error("insert into persona_assets (persona_id, asset_type, name, storage_path)"
                              " values (%s, 'face_ref', 'x', %s)", (pid_a, path))
        assert err.sqlstate == "PT422", path
    db.conn.execute("insert into persona_assets (persona_id, asset_type, name, storage_path)"
                    " values (%s, 'face_ref', 'ok', %s)", (pid_a, f"persona/{pid_a}/refs/ok.png"))
    db.conn.execute("insert into persona_assets (persona_id, asset_type, name) values (%s, 'lora', 'gina_v3.safetensors')", (pid_a,))


def test_functions_created_later_are_closed_by_default(db):
    """HIGH 2: 이후 마이그레이션에서 만든 함수도 anon·authenticated가 실행할 수 없다."""
    db.as_postgres()
    db.conn.execute("create function public.future_rpc() returns int language sql as 'select 1'")
    assert db.val("select has_function_privilege('anon', 'public.future_rpc()', 'execute')") is False
    assert db.val("select has_function_privilege('authenticated', 'public.future_rpc()', 'execute')") is False


def test_retry_step_requires_retryable_parent(db, op):
    """MEDIUM 4: 상위 Content Job이 취소됐으면 단계를 다시 실행할 수 없다."""
    uid, pid = op
    cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
    gj = create_job(db, "generation", pid, cj["id"])
    db.as_service("python")
    g = claim(db, gj["id"])
    db.val("select fail_automation_job(%s, %s, 'validation', 'LORA_NOT_FOUND', 'x', false)", (g["id"], g["locked_at"]))
    db.as_operator(uid)
    db.one("select * from cancel_content_job(%s)", (cj["id"],))
    assert db.expect_error("select retry_automation_job(%s)", (gj["id"],)).sqlstate == "PT409"


def test_inactive_persona_cannot_create_content(db, op):
    """LOW 5: 비활성 Persona로는 콘텐츠를 만들 수 없다."""
    uid, pid = op
    db.as_operator(uid)
    db.conn.execute("update personas set status = 'inactive' where id = %s", (pid,))
    assert db.expect_error("select create_content_job(%s, 'image', 'x')", (pid,)).sqlstate == "PT422"


def test_recovery_keeps_system_actor_across_nested_rollups(db, op):
    """LOW 7: 중첩 Rollup 뒤에도 다음 회수 기록의 행위자가 system으로 남는다."""
    uid, pid = op
    jobs = []
    for _ in range(2):
        cj = to_generating(db, uid, pid, p_prompt="Gina in Tokyo")
        gj = create_job(db, "generation", pid, cj["id"], max_attempts=1)
        db.as_service("python")
        jobs.append(claim(db, gj["id"])["id"])
    db.as_postgres()
    db.conn.execute("update automation_jobs set heartbeat_at = now() - interval '10 minutes' where id = any(%s)", (jobs,))
    db.as_service("python")  # 헤더가 python이어도 회수 기록은 system이어야 한다
    assert db.val("select recover_stale_jobs()") == 2
    actors = db.all("select actor_type from state_transitions where entity_id = any(%s) and to_status = 'failed'", (jobs,))
    assert [a["actor_type"] for a in actors] == ["system", "system"]
