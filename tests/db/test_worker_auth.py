"""0011 worker_auth (TECH_DESIGN 56.5): Worker JWT 신원·폐기·만료와 pa_worker 권한 격리.

이 파일이 Phase D 스파이크의 DB 절반이다. 검증하는 것:
  * 신원은 JWT 클레임에서만 온다 (RPC 인자로 넘긴 값은 무시된다)
  * 자격이 없거나·만료·폐기·Target 불일치면 아무 Job도 가져갈 수 없다
  * pa_worker는 테이블을 직접 읽지 못하고 허용된 RPC만 실행한다 (SNS 토큰 포함)

검증하지 못하는 것(실제 Supabase 프로젝트 필요): 호스팅된 PostgREST가 role=pa_worker 클레임을 받아
authenticator에서 pa_worker로 전환하는지. docs/architecture/execution-targets.md의 "JWT 역할 스파이크" 참고.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.db.test_db import create_job, to_generating

PERMISSION_DENIED = "42501"
FORBIDDEN = "PT403"


@pytest.fixture
def admin(db):
    uid = db.make_operator()
    db.as_postgres()
    db.conn.execute("update users set role = 'admin' where id = %s", (uid,))
    return uid


def issue(db, admin, worker_id="python:local-1", target="local", provider="local", hours=8):
    jti = uuid.uuid4()
    db.as_operator(admin)
    db.one(
        "select * from issue_worker_credential(%s, %s, %s, now() + make_interval(hours => %s), %s)",
        (jti, worker_id, target, hours, provider),
    )
    return jti


def claims(jti, worker_id="python:local-1", target="local", provider="local", exp_in=3600):
    return {
        "jti": str(jti),
        "worker_id": worker_id,
        "target": target,
        "provider": provider,
        "exp": int(time.time()) + exp_in,
    }


def generation_job(db, uid, target="local", pid=None):
    """generating 상태의 Content Job 아래 pending generation Job. 같은 Persona를 재사용하려면 pid를 넘긴다."""
    pid = pid or db.make_persona(uid)
    cj = to_generating(db, uid, pid, p_execution_target=target)
    job = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
    return pid, cj, job


# -----------------------------------------------------------------------------
# 자격 발급
# -----------------------------------------------------------------------------
def test_only_admin_can_issue_a_credential(db):
    uid = db.make_operator()
    db.as_operator(uid)
    assert db.expect_error(
        "select issue_worker_credential(%s, 'python:local-1', 'local', now() + interval '1 hour', 'local')",
        (uuid.uuid4(),),
    ).sqlstate == "PT403"


def test_credential_lifetime_is_capped_at_24_hours(db, admin):
    db.as_operator(admin)
    assert db.expect_error(
        "select issue_worker_credential(%s, 'python:cloud-1', 'cloud', now() + interval '25 hours', 'runpod')",
        (uuid.uuid4(),),
    ).sqlstate == "PT422"


def test_signing_secret_is_never_stored(db, admin):
    """대장에는 jti·신원·수명만 남는다. 토큰이나 서명 키는 저장하지 않는다 (17조)."""
    issue(db, admin)
    db.as_postgres()
    cols = db.val(
        "select array_agg(column_name::text) from information_schema.columns"
        " where table_schema = 'public' and table_name = 'worker_credentials'"
    )
    assert not [c for c in cols if any(w in c for w in ("secret", "token", "jwt", "signature", "key"))]


# -----------------------------------------------------------------------------
# 신원은 클레임에서만 온다
# -----------------------------------------------------------------------------
def test_valid_worker_jwt_claims_a_matching_job(db, admin):
    _, _, job = generation_job(db, admin)
    jti = issue(db, admin)

    db.as_worker(**claims(jti))
    row = db.one("select * from claim_next_automation_job('generation', 'python:local-1')")
    assert row["id"] == job["id"] and row["claimed_by"] == "python:local-1"


def test_worker_identity_comes_from_claims_not_from_arguments(db, admin):
    """인자로 다른 워커 이름을 보내도 클레임의 신원이 기록된다 (6조)."""
    _, _, job = generation_job(db, admin)
    jti = issue(db, admin, worker_id="python:local-1")

    db.as_worker(**claims(jti, worker_id="python:local-1"))
    row = db.one("select * from claim_automation_job(%s, 'python:someone-else')", (job["id"],))
    assert row["claimed_by"] == "python:local-1"


def test_a_cloud_credential_cannot_claim_a_local_job(db, admin):
    _, _, job = generation_job(db, admin, target="local")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")

    db.as_worker(**claims(jti, worker_id="python:cloud-1", target="cloud", provider="runpod"))
    assert db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (job["id"],)) is None


def test_a_cloud_credential_claims_a_cloud_job(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")

    db.as_worker(**claims(jti, worker_id="python:cloud-1", target="cloud", provider="runpod"))
    row = db.one("select * from claim_automation_job(%s, 'python:cloud-1')", (job["id"],))
    assert row["id"] == job["id"] and row["claimed_by"] == "python:cloud-1"


# -----------------------------------------------------------------------------
# 깨진 자격은 아무것도 가져가지 못한다
# -----------------------------------------------------------------------------
def claim_is_refused(db, job, *, worker="python:local-1"):
    """깨진 신원은 **오류(FORBIDDEN)** 로 거부되고, Job 상태·시도 횟수는 그대로여야 한다.

    예전에는 빈 결과로 넘겼다. 그러면 워커가 같은 자격으로 계속 두드리고, local Job은 폴백으로 가져갈 수 있었다.
    오류여야 Python이 자격 거부로 알아보고 선점을 멈춘다 (사양 21조).
    """
    assert db.expect_error("select * from claim_automation_job(%s, %s)", (job["id"], worker)).sqlstate == FORBIDDEN
    assert db.expect_error("select * from claim_next_automation_job('generation', %s)", (worker,)).sqlstate == FORBIDDEN
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "pending"
    assert db.val("select attempts from automation_jobs where id = %s", (job["id"],)) == 0


def test_missing_claims_are_rejected(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    db.as_worker()                                   # jti·worker_id·target 없음
    claim_is_refused(db, job)


def test_an_unknown_jti_is_rejected(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    db.as_worker(**claims(uuid.uuid4(), target="cloud"))
    claim_is_refused(db, job)


def test_a_revoked_credential_is_rejected(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud")

    db.as_operator(admin)
    db.one("select * from revoke_worker_credential(%s)", (jti,))

    db.as_worker(**claims(jti, worker_id="python:cloud-1", target="cloud"))
    claim_is_refused(db, job, worker="python:cloud-1")


def test_an_expired_credential_is_rejected(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud")

    db.as_postgres()
    db.conn.execute(
        "update worker_credentials set issued_at = now() - interval '10 hours',"
        " expires_at = now() - interval '1 hour' where jti = %s", (jti,))

    db.as_worker(**claims(jti, worker_id="python:cloud-1", target="cloud"))
    claim_is_refused(db, job, worker="python:cloud-1")


def test_an_expired_token_is_rejected_even_if_the_credential_is_live(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud")

    db.as_worker(**claims(jti, worker_id="python:cloud-1", target="cloud", exp_in=-60))
    claim_is_refused(db, job, worker="python:cloud-1")


def test_claims_that_disagree_with_the_credential_are_rejected(db, admin):
    _, _, job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud")

    # Target을 바꿔 치면 거부된다
    db.as_worker(**claims(jti, worker_id="python:cloud-1", target="local"))
    claim_is_refused(db, job, worker="python:cloud-1")

    # 워커 신원을 바꿔 치면 거부된다
    db.as_worker(**claims(jti, worker_id="python:somebody", target="cloud"))
    claim_is_refused(db, job, worker="python:cloud-1")


# -----------------------------------------------------------------------------
# pa_worker 권한 격리
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "table",
    ["social_accounts", "personas", "assets", "content_jobs", "automation_jobs",
     "app_settings", "users", "worker_credentials", "posts"],
)
def test_worker_cannot_read_tables_directly(db, admin, table):
    jti = issue(db, admin)
    db.as_worker(**claims(jti))
    assert db.expect_error(f"select * from {table} limit 1").sqlstate == PERMISSION_DENIED


def test_worker_cannot_write_or_delete_directly(db, admin):
    _, _, job = generation_job(db, admin)
    jti = issue(db, admin)
    db.as_worker(**claims(jti))
    for sql in (
        "delete from automation_jobs",
        "update content_jobs set status = 'ready'",
        "insert into assets (persona_id, content_job_id, asset_type, file_name, storage_path, mime_type)"
        " values (gen_random_uuid(), gen_random_uuid(), 'image', 'x.png', 'p/x.png', 'image/png')",
        "update app_settings set value = '\"cloud\"'::jsonb where key = 'default_execution_target'",
    ):
        assert db.expect_error(sql).sqlstate == PERMISSION_DENIED, sql


@pytest.mark.parametrize(
    "sql",
    [
        "select create_content_job(p_persona_id => gen_random_uuid(), p_content_type => 'image')",
        "select move_pending_jobs_to_target('cloud')",
        "select update_app_setting('default_execution_target', '\"cloud\"'::jsonb)",
        "select get_app_settings()",
        "select list_worker_credentials()",
        "select issue_worker_credential(gen_random_uuid(), 'python:x', 'cloud', now() + interval '1 hour', 'runpod')",
        "select retry_automation_job(gen_random_uuid())",
        "select recover_stale_jobs()",
        "select create_post_draft(gen_random_uuid(), now(), gen_random_uuid(), 'x', 'y', array['z'])",
        "select sync_workflow_registry('[]'::jsonb)",
    ],
)
def test_worker_cannot_execute_unauthorized_rpcs(db, admin, sql):
    jti = issue(db, admin)
    db.as_worker(**claims(jti))
    assert db.expect_error(sql).sqlstate == PERMISSION_DENIED, sql


# -----------------------------------------------------------------------------
# worker_get_job_context: 필요한 것만
# -----------------------------------------------------------------------------
def test_job_context_gives_execution_data_and_no_secrets(db, admin):
    pid, cj, job = generation_job(db, admin)
    db.as_postgres()
    db.conn.execute(
        "insert into social_accounts (persona_id, platform, account_id, username, access_token_secret_id)"
        " values (%s, 'instagram', 'acct-1', 'gina', gen_random_uuid())", (pid,))

    jti = issue(db, admin)
    db.as_worker(**claims(jti))
    db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],))
    ctx = db.val("select worker_get_job_context(%s)", (job["id"],))

    assert ctx["job"]["id"] == str(job["id"]) and ctx["job"]["execution_target"] == "local"
    assert ctx["content_job"]["id"] == str(cj["id"])
    assert ctx["persona"]["id"] == str(pid)

    blob = str(ctx).lower()
    for leaked in ("access_token", "refresh_token", "secret", "allowed_emails", "service_role"):
        assert leaked not in blob, leaked
    # 대화용 Persona 설정은 실행에 필요 없다
    assert "personality" not in ctx["persona"] and "speaking_style" not in ctx["persona"]


def test_worker_cannot_read_the_context_of_a_job_it_did_not_claim(db, admin):
    _, _, mine = generation_job(db, admin)
    jti = issue(db, admin)
    db.as_worker(**claims(jti))
    # 선점하지 않은 Job (아직 pending)
    assert db.expect_error("select worker_get_job_context(%s)", (mine["id"],)).sqlstate == "PT403"

    # 없는 Job도 같은 오류다: 워커에게는 Job이 존재하는지조차 알려 주지 않는다
    db.as_worker(**claims(jti))
    assert db.expect_error("select worker_get_job_context(%s)", (uuid.uuid4(),)).sqlstate == "PT403"


def test_a_worker_cannot_read_a_job_claimed_by_another_worker(db, admin):
    _, _, job = generation_job(db, admin)
    jti_a = issue(db, admin, worker_id="python:local-1")
    jti_b = issue(db, admin, worker_id="python:local-2")

    db.as_worker(**claims(jti_a, worker_id="python:local-1"))
    db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],))

    db.as_worker(**claims(jti_b, worker_id="python:local-2"))
    assert db.expect_error("select worker_get_job_context(%s)", (job["id"],)).sqlstate == "PT403"


# -----------------------------------------------------------------------------
# 폐기
# -----------------------------------------------------------------------------
def test_a_worker_may_revoke_only_its_own_credential(db, admin):
    jti_a = issue(db, admin, worker_id="python:local-1")
    jti_b = issue(db, admin, worker_id="python:local-2")

    db.as_worker(**claims(jti_a))
    assert db.expect_error("select revoke_worker_credential(%s)", (jti_b,)).sqlstate == "PT403"

    db.as_worker(**claims(jti_a))
    assert db.one("select * from revoke_worker_credential(%s)", (jti_a,))["revoked_at"] is not None


def test_revocation_stops_every_further_rpc(db, admin):
    _, _, job = generation_job(db, admin)
    jti = issue(db, admin)

    db.as_worker(**claims(jti))
    db.one("select * from claim_automation_job(%s, 'python:local-1')", (job["id"],))

    db.as_operator(admin)
    db.one("select * from revoke_worker_credential(%s)", (jti,))

    db.as_worker(**claims(jti))
    assert db.expect_error("select worker_get_job_context(%s)", (job["id"],)).sqlstate == "PT403"


def test_the_mint_script_produces_claims_the_database_accepts(db, admin):
    """scripts/mint_worker_token.py가 만드는 클레임이 private.worker_identity()와 맞는지 (56.5)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from mint_worker_token import worker_claims

    _, _, job = generation_job(db, admin, target="cloud")
    jti = issue(db, admin, worker_id="python:cloud-1", target="cloud", provider="runpod")
    now = int(time.time())
    minted = worker_claims("python:cloud-1", "cloud", "runpod", jti, now, now + 3600)

    db.as_worker(**{k: v for k, v in minted.items() if k != "role"})
    row = db.one("select * from claim_automation_job(%s, 'ignored')", (job["id"],))
    assert row is not None and row["claimed_by"] == "python:cloud-1"


WORKER_RPC_WHITELIST = {
    "claim_next_automation_job", "claim_automation_job", "heartbeat_automation_job",
    "complete_automation_job", "fail_automation_job", "register_asset", "log_execution",
    "report_worker_status", "log_security_event", "worker_get_job_context",
    "worker_get_input_asset", "revoke_worker_credential",
}


def test_pa_worker_has_no_table_privileges(db):
    db.as_postgres()
    assert db.val(
        "select count(*) from information_schema.role_table_grants"
        " where grantee = 'pa_worker' and table_schema in ('public', 'private')") == 0


# private 스키마는 PostgREST에 노출되지 않는다. 이 둘은 storage.objects 정책이 평가하는 술어라서 EXECUTE가 필요하다.
STORAGE_POLICY_PREDICATES = {"worker_may_read_object", "worker_may_write_object"}


def executable_by(db, role, schema):
    db.as_postgres()
    return set(db.val(
        "select coalesce(array_agg(p.proname::text), '{}') from pg_proc p"
        "  join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname = %s and has_function_privilege(%s, p.oid, 'EXECUTE')", (schema, role)) or [])


def test_pa_worker_execute_surface_is_exactly_the_whitelist(db):
    """Postgres는 함수에 PUBLIC EXECUTE를 기본 부여한다 → 화이트리스트 밖이 새지 않는지 확인한다 (12조)."""
    reachable = executable_by(db, "pa_worker", "public")
    assert reachable - WORKER_RPC_WHITELIST == set(), \
        f"pa_worker can execute functions outside the whitelist: {sorted(reachable - WORKER_RPC_WHITELIST)}"
    # 반대 방향도 확인한다: 권한이 빠져 워커가 일을 못 하게 되는 것도 회귀다
    assert WORKER_RPC_WHITELIST - reachable == set(), \
        f"pa_worker is missing EXECUTE on: {sorted(WORKER_RPC_WHITELIST - reachable)}"
    assert executable_by(db, "pa_worker", "private") == STORAGE_POLICY_PREDICATES


def test_revocation_is_audited(db, admin):
    jti = issue(db, admin)
    db.as_operator(admin)
    db.one("select * from revoke_worker_credential(%s)", (jti,))
    db.as_postgres()
    event = db.one("select event_type, detail from security_events order by id desc limit 1")
    assert event["event_type"] == "WORKER_CREDENTIAL_REVOKED" and event["detail"]["jti"] == str(jti)
