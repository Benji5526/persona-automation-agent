"""최종 보안 게이트: 로컬·클라우드 Worker의 선점 권한 감사 (TECH_DESIGN 56.4, 56.5).

답해야 하는 질문 (게이트 1번)
  1. 무엇이 로컬 워커를 인증하나?           → service_role(Phase 0) 또는 pa_worker + Worker JWT
  2. worker_status 등록이 필수인가?          → pa_worker: 아니오(자격이 신원). service_role: 아니오(아래 참고)
  3. 등록이 필수가 아니면 무엇이 막나?       → claim RPC는 service_role·pa_worker에게만 EXECUTE가 있다
  4. pa_worker 신원 + 자격 + Target 일치는 충분한가? → 아래 테스트 전부
  5. 다른 worker_id를 사칭할 수 있나?        → 아니오 (클레임이 신원, 인자는 무시)
  6. target=cloud가 local Job을 선점하나?    → 아니오
  7. 인증된 일반 사용자가 claim RPC를 부르나? → 아니오 (EXECUTE 없음)
  8. 폐기·만료된 자격이 선점하나?            → 아니오 (오류, 빈 결과가 아니다)
  9. 등록되지 않은 워커가 선점하나?          → pa_worker는 자격이 필요. service_role은 local만 (아래 참고)

service_role 경로(Phase 0)의 의도적 완화: F0 테스트(test_concurrent_generation_job_claim_has_one_winner)는
등록되지 않은 service_role 워커가 local Job을 선점하는 것을 요구한다. service_role은 어차피 모든 테이블을
읽고 쓸 수 있는 전권 키이고 report_worker_status로 스스로 등록할 수 있으므로, 등록을 강제해도 보안이 늘지 않는다.
그래서 service_role은 'local'로만 취급한다. 실제 경계는 pa_worker 경로이고, 여기서는 자격이 필수다.
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.db.test_db import create_job, to_generating
from tests.db.test_worker_auth import admin, claims, generation_job, issue  # noqa: F401  (fixture 포함)

PERMISSION_DENIED = "42501"
FORBIDDEN = "PT403"


def as_cloud(db, jti):
    return db.as_worker(**claims(jti, worker_id="python:cloud-1", target="cloud", provider="runpod"))


def issue_cloud(db, admin):
    return issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")


# -----------------------------------------------------------------------------
# 7. 인증된 일반 사용자·익명은 claim RPC를 부를 수 없다
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("who", ["anon", "operator"])
@pytest.mark.parametrize(
    "sql",
    [
        "select * from claim_next_automation_job('generation', 'python:x')",
        "select * from claim_automation_job(gen_random_uuid(), 'python:x')",
        "select worker_get_job_context(gen_random_uuid())",
        "select worker_get_input_asset(gen_random_uuid(), gen_random_uuid())",
        "select report_worker_status('python:x', 'python', '{}'::jsonb)",
        "select log_execution(gen_random_uuid(), 's', 'python', 'started')",
        "select log_security_event('X', 'python')",
        "select register_asset(gen_random_uuid(), now(), '{}'::jsonb)",
        "select heartbeat_automation_job(gen_random_uuid(), now())",
        "select complete_automation_job(gen_random_uuid(), now())",
        "select fail_automation_job(gen_random_uuid(), now(), 'unknown', 'X', 'm', false)",
    ],
)
def test_non_workers_cannot_call_worker_rpcs(db, admin, who, sql):
    if who == "anon":
        db.as_anon()
    else:
        db.as_operator(admin)          # 심지어 admin 소유자도 워커 RPC는 못 부른다
    assert db.expect_error(sql).sqlstate == PERMISSION_DENIED, sql


# -----------------------------------------------------------------------------
# 8·1·4. 깨진 자격은 **오류**다 — 빈 결과로 조용히 넘기지 않고, local Job도 가져가지 못한다
# -----------------------------------------------------------------------------
def break_credential(db, admin, how, jti):
    if how == "revoked":
        db.as_operator(admin)
        db.one("select * from revoke_worker_credential(%s)", (jti,))
    elif how == "expired":
        db.as_postgres()
        db.conn.execute("update worker_credentials set issued_at = now() - interval '10 hours',"
                        " expires_at = now() - interval '1 hour' where jti = %s", (jti,))


@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_a_broken_credential_cannot_claim_even_a_local_job(db, admin, how):
    """local Job이라고 예외가 아니다. 신원이 깨지면 local로 폴백하지 않는다 (게이트에서 발견한 구멍)."""
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin, worker_id="python:local-1", target="local")
    break_credential(db, admin, how, jti)

    db.as_worker(**claims(jti))
    assert db.expect_error("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)).sqlstate == FORBIDDEN
    db.as_worker(**claims(jti))
    assert db.expect_error("select * from claim_next_automation_job('generation', 'python:local-1')").sqlstate == FORBIDDEN
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "pending"
    assert db.val("select attempts from automation_jobs where id = %s", (job["id"],)) == 0


def test_an_expired_token_cannot_claim_a_local_job(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin)
    db.as_worker(**claims(jti, exp_in=-60))
    assert db.expect_error("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)).sqlstate == FORBIDDEN


@pytest.mark.parametrize("bad", [{}, {"jti": "not-a-uuid", "worker_id": "python:local-1", "target": "local"},
                                 {"jti": str(uuid.uuid4()), "worker_id": "python:local-1", "target": "local",
                                  "exp": 4102444800}])
def test_missing_or_unknown_claims_cannot_claim_a_local_job(db, admin, bad):
    _, _, job = generation_job(db, admin, target="local")
    db.as_worker(**bad)
    assert db.expect_error("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)).sqlstate == FORBIDDEN


# -----------------------------------------------------------------------------
# 5·6·G·H. 신원·Target 격리
# -----------------------------------------------------------------------------
def test_target_isolation_both_ways_with_valid_identities(db, admin):
    pid, _, local_job = generation_job(db, admin, target="local")
    _, _, cloud_job = generation_job(db, admin, target="cloud", pid=pid)
    jti_local = issue(db, admin, worker_id="python:local-1", target="local")
    jti_cloud = issue_cloud(db, admin)

    db.as_worker(**claims(jti_cloud, worker_id="python:cloud-1", target="cloud", provider="runpod"))
    assert db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (local_job["id"],)) is None   # E
    db.as_worker(**claims(jti_local))
    assert db.one("select * from claim_automation_job(%s, 'python:local-1')", (cloud_job["id"],)) is None   # D
    db.as_worker(**claims(jti_cloud, worker_id="python:cloud-1", target="cloud", provider="runpod"))
    assert db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (cloud_job["id"],))["status"] == "processing"


def test_provider_metadata_never_overrides_target_authorization(db, admin):
    """F: 상태 보고에 provider·target을 속여 써도 자격의 Target이 전부다."""
    _, _, cloud_job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:local-1", target="local", provider="local")

    db.as_worker(**claims(jti))
    db.val("select report_worker_status('python:local-1', 'python', %s::jsonb)",
           (json.dumps({"target": "cloud", "provider": "runpod"}),))
    db.as_postgres()
    stored = db.one("select target, provider from worker_status where id = 'python:local-1'")
    assert (stored["target"], stored["provider"]) == ("local", "local")      # 본문 값은 무시된다

    db.as_worker(**claims(jti))
    assert db.one("select * from claim_automation_job(%s, 'x')", (cloud_job["id"],)) is None


def test_a_worker_cannot_report_status_for_another_worker(db, admin):
    jti = issue(db, admin, worker_id="python:local-1", target="local")
    db.as_service("python")
    db.val("select report_worker_status('python:victim', 'python', %s::jsonb)",
           (json.dumps({"target": "local", "queue_size": 3, "comfyui_ok": True}),))

    db.as_worker(**claims(jti))
    assert db.expect_error(
        "select report_worker_status('python:victim', 'python', %s::jsonb)",
        (json.dumps({"target": "cloud", "comfyui_ok": False}),)).sqlstate == FORBIDDEN
    db.as_postgres()
    victim = db.one("select target, comfyui_ok, queue_size from worker_status where id = 'python:victim'")
    assert victim == {"target": "local", "comfyui_ok": True, "queue_size": 3}   # 건드려지지 않았다


def test_a_worker_may_only_report_itself_as_a_python_worker(db, admin):
    jti = issue(db, admin, worker_id="python:local-1", target="local")
    db.as_worker(**claims(jti))
    assert db.expect_error("select report_worker_status('python:local-1', 'n8n', '{}'::jsonb)").sqlstate == FORBIDDEN
    db.as_worker(**claims(jti))
    db.val("select report_worker_status('python:local-1', 'python', '{\"queue_size\": 1}'::jsonb)")
    db.as_postgres()
    assert db.val("select kind from worker_status where id = 'python:local-1'") == "python"


# -----------------------------------------------------------------------------
# 9. 등록되지 않은 워커
# -----------------------------------------------------------------------------
def test_an_unregistered_service_role_worker_claims_local_only(db, admin):
    """Phase 0 경로: 등록 없이 local은 가져가고 cloud는 못 가져간다 (F0 테스트와 같은 전제)."""
    pid, _, local_job = generation_job(db, admin, target="local")
    _, _, cloud_job = generation_job(db, admin, target="cloud", pid=pid)
    db.as_service("python")
    assert db.one("select * from claim_automation_job(%s, 'python:never-registered')", (cloud_job["id"],)) is None
    assert db.one("select * from claim_automation_job(%s, 'python:never-registered')", (local_job["id"],))["status"] == "processing"


def test_a_service_role_worker_cannot_claim_cloud_by_naming_itself_cloud(db, admin):
    _, _, cloud_job = generation_job(db, admin, target="cloud")
    db.as_service("python")
    assert db.one("select * from claim_next_automation_job('generation', 'python:cloud-1')") is None
    assert db.one("select * from claim_automation_job(%s, null)", (cloud_job["id"],)) is None


# -----------------------------------------------------------------------------
# pa_worker는 generation Job만 다룬다 (publish·caption·prompt 불가)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("job_type", ["prompt"])
def test_a_worker_cannot_claim_non_generation_jobs(db, admin, job_type):
    pid = db.make_persona(admin)
    cj = to_generating(db, admin, pid)
    job = create_job(db, job_type, pid, cj["id"], key=f"{job_type}:{cj['id']}:1")
    jti = issue(db, admin)

    db.as_worker(**claims(jti))
    assert db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],)) is None
    db.as_worker(**claims(jti))
    assert db.one("select * from claim_next_automation_job(%s, 'python:local-1')", (job_type,)) is None
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "pending"


# -----------------------------------------------------------------------------
# 5. 다른 워커의 Job을 건드릴 수 없다 (자격이 맞아도, locked_at을 알아도)
# -----------------------------------------------------------------------------
def claimed_by_worker_a(db, admin):
    pid, cj, job = generation_job(db, admin)
    jti_a = issue(db, admin, worker_id="python:local-1", target="local")
    jti_b = issue(db, admin, worker_id="python:local-2", target="local")
    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    row = db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],))
    return pid, cj, row, jti_a, jti_b


def test_another_worker_cannot_heartbeat_complete_fail_or_log_on_a_claimed_job(db, admin):
    _, _, row, _, jti_b = claimed_by_worker_a(db, admin)
    leaked_lock = row["locked_at"]               # 최악의 경우: locked_at이 새어 나갔다

    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.val("select heartbeat_automation_job(%s, %s)", (row["id"], leaked_lock)) is False
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.val("select complete_automation_job(%s, %s)", (row["id"], leaked_lock)) is False
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    res = db.val("select fail_automation_job(%s, %s, 'unknown', 'X', 'sabotage', false)", (row["id"], leaked_lock))
    assert res["applied"] is False
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.expect_error("select log_execution(%s, 'FORGED', 'python', 'succeeded')", (row["id"],)).sqlstate == FORBIDDEN
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.expect_error("select worker_get_job_context(%s)", (row["id"],)).sqlstate == FORBIDDEN

    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (row["id"],)) == "processing"
    assert db.val("select claimed_by from automation_jobs where id = %s", (row["id"],)) == "python:local-1"
    assert db.val("select count(*) from execution_logs where step = 'FORGED'") == 0


def test_another_worker_cannot_register_an_asset_on_a_claimed_job(db, admin):
    pid, _, row, _, jti_b = claimed_by_worker_a(db, admin)
    asset_id = uuid.uuid4()
    asset = {"id": str(asset_id), "asset_type": "image", "file_name": "x.png", "mime_type": "image/png",
             "storage_path": f"persona/{pid}/assets/{asset_id}.png"}
    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.one("select * from register_asset(%s, %s, %s::jsonb)", (row["id"], row["locked_at"], json.dumps(asset))) is None
    db.as_postgres()
    assert db.val("select count(*) from assets") == 0


def test_the_owner_worker_can_still_do_all_of_it(db, admin):
    _, _, row, jti_a, _ = claimed_by_worker_a(db, admin)
    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    assert db.val("select heartbeat_automation_job(%s, %s)", (row["id"], row["locked_at"])) is True
    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    db.val("select log_execution(%s, 'BUILD', 'python', 'started')", (row["id"],))
    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    assert db.val("select worker_get_job_context(%s)", (row["id"],))["job"]["id"] == str(row["id"])


# -----------------------------------------------------------------------------
# 자격 관리는 admin(소유자)만, 워커는 자기 자격만
# -----------------------------------------------------------------------------
def test_a_plain_operator_cannot_manage_credentials(db, admin):
    jti = issue(db, admin)
    plain = db.make_operator()                                  # admin이 아닌 일반 operator
    db.as_operator(plain)
    assert db.expect_error("select * from revoke_worker_credential(%s)", (jti,)).sqlstate == FORBIDDEN
    db.as_operator(plain)
    assert db.expect_error("select * from list_worker_credentials()").sqlstate == FORBIDDEN
    db.as_operator(plain)
    assert db.expect_error(
        "select issue_worker_credential(%s, 'python:x', 'local', now() + interval '1 hour', 'local')",
        (uuid.uuid4(),)).sqlstate == FORBIDDEN
    db.as_postgres()
    assert db.val("select revoked_at is null from worker_credentials where jti = %s", (jti,)) is True


def test_a_worker_with_a_broken_credential_cannot_revoke_anything(db, admin):
    """폐기된 워커가 다른 워커의 자격을 폐기하거나 자기 자격 상태를 건드릴 수 없다."""
    victim = issue(db, admin, worker_id="python:local-2", target="local")
    mine = issue(db, admin, worker_id="python:local-1", target="local")
    db.as_operator(admin)
    db.one("select * from revoke_worker_credential(%s)", (mine,))

    db.as_worker(**claims(mine))
    assert db.expect_error("select * from revoke_worker_credential(%s)", (victim,)).sqlstate == FORBIDDEN
    db.as_postgres()
    assert db.val("select revoked_at is null from worker_credentials where jti = %s", (victim,)) is True


# -----------------------------------------------------------------------------
# 감사 로그는 위조할 수 없다
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("event", ["WORKER_CREDENTIAL_REVOKED", "PUBLISHING_ENABLED", "EXECUTION_TARGET_JOBS_MOVED",
                                   "SETTINGS_CHANGED", "ANYTHING_ELSE"])
def test_a_worker_cannot_forge_security_events(db, admin, event):
    jti = issue(db, admin)
    db.as_worker(**claims(jti))
    assert db.expect_error("select log_security_event(%s, 'operator')", (event,)).sqlstate == FORBIDDEN


def test_worker_security_events_are_forced_to_actor_python_and_own_persona(db, admin):
    pid, _, row, jti_a, _ = claimed_by_worker_a(db, admin)
    other_persona = db.make_persona(db.make_operator())
    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    # 다른 Persona에 대한 이벤트는 거부
    assert db.expect_error("select log_security_event('OUTPUT_UNEXPECTED', 'python', null, '{}'::jsonb, %s)",
                           (other_persona,)).sqlstate == FORBIDDEN
    # 자기 Job의 Persona: 허용. actor_type을 operator로 속여도 python으로 기록된다
    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    db.val("select log_security_event('OUTPUT_UNEXPECTED', 'operator', null, '{\"job_id\": \"x\"}'::jsonb, %s)", (pid,))
    db.as_postgres()
    event = db.one("select actor_type, persona_id from security_events where event_type = 'OUTPUT_UNEXPECTED'"
                   " order by id desc limit 1")
    assert event["actor_type"] == "python" and str(event["persona_id"]) == str(pid)
