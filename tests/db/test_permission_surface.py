"""권한 표면 감사 (최종 게이트 8번): 문서가 아니라 **실제 DB 상태**에서 역할별 권한을 꺼내 정확히 비교한다.

  python -m pytest tests/db/test_permission_surface.py -q -s     # -s 로 실제 허용·거부 목록을 출력한다

ALLOWLIST: 이 목록과 정확히 같아야 한다 (남아도, 모자라도 실패한다).
DENYLIST : 어떤 경우에도 pa_worker가 실행하면 안 되는 함수. 목록에 없는 새 함수가 생겨도 ALLOWLIST 검사가 잡는다.
"""

from __future__ import annotations

import pytest

# pa_worker가 PostgREST로 부를 수 있는 RPC의 전부 (0011 9절)
PA_WORKER_RPC_ALLOWLIST = {
    "claim_next_automation_job", "claim_automation_job", "heartbeat_automation_job",
    "complete_automation_job", "fail_automation_job", "register_asset", "log_execution",
    "report_worker_status", "log_security_event", "worker_get_job_context",
    "worker_get_input_asset", "revoke_worker_credential",
}
# private 스키마는 PostgREST에 노출되지 않는다. storage.objects 정책이 평가하는 술어 둘뿐이다.
PA_WORKER_PRIVATE_ALLOWLIST = {"worker_may_read_object", "worker_may_write_object"}

# pa_worker가 절대 실행하면 안 되는 것
PA_WORKER_RPC_DENYLIST = {
    "create_content_job", "submit_content_job", "cancel_content_job", "retry_content_job",
    "regenerate_content_job", "retry_automation_job", "archive_asset", "resolve_system_error",
    "get_app_settings", "update_app_setting", "get_dashboard_summary",
    "issue_worker_credential", "list_worker_credentials", "move_pending_jobs_to_target", "review_asset", "emergency_stop_all",
    "create_post_from_asset", "submit_post_for_approval", "resolve_approval", "revise_post", "cancel_post",
    "claim_content_job", "create_automation_job", "save_prompt_parts", "create_post_draft",
    "sync_workflow_registry", "recover_stale_jobs", "reserve_llm_call", "dispatch_queued_content_jobs",
}

# Lovable(authenticated)이 부를 수 있는 RPC의 전부. 워커 RPC는 하나도 없다.
AUTHENTICATED_RPC_ALLOWLIST = {
    "archive_asset", "cancel_content_job", "create_content_job", "get_app_settings", "get_dashboard_summary",
    "issue_worker_credential", "list_worker_credentials", "move_pending_jobs_to_target",
    "regenerate_content_job", "resolve_system_error", "retry_automation_job", "retry_content_job",
    "revoke_worker_credential", "review_asset", "emergency_stop_all", "submit_content_job", "update_app_setting",
    "create_post_from_asset", "submit_post_for_approval", "resolve_approval", "revise_post", "cancel_post",
}
WORKER_ONLY_RPCS = {
    "claim_next_automation_job", "claim_automation_job", "heartbeat_automation_job", "complete_automation_job",
    "fail_automation_job", "register_asset", "log_execution", "report_worker_status", "log_security_event",
    "worker_get_job_context", "worker_get_input_asset", "claim_content_job", "create_automation_job",
    "save_prompt_parts", "create_post_draft", "sync_workflow_registry", "recover_stale_jobs", "reserve_llm_call", "dispatch_queued_content_jobs",
}


def executable(db, role, schema):
    db.as_postgres()
    return set(db.val(
        "select coalesce(array_agg(p.proname::text), '{}') from pg_proc p"
        "  join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname = %s and has_function_privilege(%s, p.oid, 'EXECUTE')", (schema, role)) or [])


def table_privileges(db, role):
    db.as_postgres()
    rows = db.conn.execute(
        "select table_schema, table_name, privilege_type from information_schema.role_table_grants"
        " where grantee = %s order by 1, 2, 3", (role,)).fetchall()
    return [(s, t, p) for s, t, p in rows]


def show(title, items):
    print(f"\n[{title}] ({len(items)})")
    for item in sorted(items):
        print("   ", item)


def test_pa_worker_permission_surface_from_the_actual_database(db):
    rpc = executable(db, "pa_worker", "public")
    priv = executable(db, "pa_worker", "private")
    tables = table_privileges(db, "pa_worker")

    show("pa_worker ALLOWLIST public RPC", rpc)
    show("pa_worker ALLOWLIST private (storage policy predicates)", priv)
    show("pa_worker table privileges", tables)

    assert rpc == PA_WORKER_RPC_ALLOWLIST, (
        f"unexpected: {sorted(rpc - PA_WORKER_RPC_ALLOWLIST)}, missing: {sorted(PA_WORKER_RPC_ALLOWLIST - rpc)}")
    assert priv == PA_WORKER_PRIVATE_ALLOWLIST

    # public·private 스키마의 테이블 권한은 0건. 허용되는 것은 storage.objects의 SELECT·INSERT뿐이다 (행은 RLS 정책이 거른다).
    assert [t for t in tables if t[0] != "storage"] == []
    assert tables == [("storage", "objects", "INSERT"), ("storage", "objects", "SELECT")]


def test_pa_worker_denylist_is_really_denied(db):
    reachable = executable(db, "pa_worker", "public")
    show("pa_worker DENYLIST (확인한 거부)", PA_WORKER_RPC_DENYLIST)
    assert reachable & PA_WORKER_RPC_DENYLIST == set(), sorted(reachable & PA_WORKER_RPC_DENYLIST)


def test_no_social_credentials_are_reachable_by_a_worker(db):
    """SNS 토큰 접근 경로가 없다: 테이블 권한도, 이를 반환하는 RPC도 없다."""
    db.as_postgres()
    for table in ("social_accounts", "posts", "users", "app_settings", "worker_credentials", "security_events"):
        assert not db.val("select has_table_privilege('pa_worker', %s, 'SELECT')", (f"public.{table}",)), table
    assert not db.val("select has_schema_privilege('pa_worker', 'private', 'USAGE')")
    assert not db.val("select has_schema_privilege('pa_worker', 'auth', 'USAGE')")
    # 워커가 받는 어떤 RPC 정의에도 SNS 토큰 칸이 나오지 않는다
    for fn in ("worker_get_job_context", "worker_get_input_asset"):
        src = db.val("select pg_get_functiondef(p.oid) from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
                     " where n.nspname = 'public' and p.proname = %s", (fn,))
        assert "social_accounts" not in src and "secret_id" not in src, fn


def test_lovable_authenticated_surface_is_operator_rpcs_only(db):
    rpc = executable(db, "authenticated", "public")
    show("authenticated ALLOWLIST", rpc)
    assert rpc == AUTHENTICATED_RPC_ALLOWLIST
    assert rpc & WORKER_ONLY_RPCS == set(), "an operator must never be able to call a worker RPC"


def test_anon_can_execute_nothing(db):
    assert executable(db, "anon", "public") == set()
    assert executable(db, "anon", "private") == set()


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_untrusted_roles_cannot_execute_private_helpers_except_the_column_default(db, role):
    assert executable(db, role, "private") - ({"default_execution_target"} if role == "authenticated" else set()) == set()


def test_pa_worker_is_a_nologin_role_without_superuser_or_bypass(db):
    db.as_postgres()
    row = db.one("select rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolinherit"
                 " from pg_roles where rolname = 'pa_worker'")
    assert row == {"rolcanlogin": False, "rolsuper": False, "rolbypassrls": False,
                   "rolcreatedb": False, "rolcreaterole": False, "rolinherit": False}
