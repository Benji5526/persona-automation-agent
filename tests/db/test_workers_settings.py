"""0007: worker_status, Settings RPC, resolve_system_error (TECH_DESIGN 17.4, 17.5, 18.9)."""

from __future__ import annotations

import json

PERMISSION_DENIED = "42501"


def test_worker_status_report_and_dashboard(db):
    uid = db.make_operator()
    db.make_persona(uid)
    db.as_service("python")
    info = {"comfyui_ok": True, "gpu": {"name": "RTX 5080", "vram_free_mb": 2100}, "queue_size": 2, "version": "1.0"}
    db.val("select report_worker_status('python:rtx5080-1', 'python', %s::jsonb)", (json.dumps(info),))
    db.val("select report_worker_status('python:rtx5080-1', 'python', %s::jsonb)", (json.dumps({**info, "queue_size": 0}),))

    db.as_operator(uid)
    row = db.one("select kind, comfyui_ok, gpu, queue_size from worker_status where id = 'python:rtx5080-1'")
    assert row == {"kind": "python", "comfyui_ok": True, "gpu": {"name": "RTX 5080", "vram_free_mb": 2100}, "queue_size": 0}
    workers = db.val("select get_dashboard_summary()")["workers"]
    assert len(workers) == 1 and workers[0]["online"] is True
    # Operator는 쓸 수 없다
    assert db.expect_error("select report_worker_status('x', 'python', '{}')").sqlstate == PERMISSION_DENIED
    assert db.expect_error("update worker_status set queue_size = 9").sqlstate == PERMISSION_DENIED


def test_settings_require_admin(db):
    uid = db.make_operator()
    db.as_operator(uid)
    assert db.expect_error("select get_app_settings()").sqlstate == "PT403"

    db.as_postgres()
    db.conn.execute("update users set role = 'admin' where id = %s", (uid,))
    db.as_operator(uid)
    settings = db.val("select get_app_settings()")
    assert set(settings) == {"allowed_emails", "limits", "publishing_enabled", "retry_backoff_seconds", "active_worker", "app_mode"}

    updated = db.val("select update_app_setting('limits', '{\"daily_generation_limit\": 50}'::jsonb)")
    assert updated["limits"]["daily_generation_limit"] == 50
    assert updated["limits"]["max_content_jobs_per_hour"] == 30  # 다른 한도는 유지
    db.val("select update_app_setting('publishing_enabled', 'true'::jsonb)")
    assert db.val("select event_type from security_events where actor_id = %s order by id desc limit 1", (uid,)) == "PUBLISHING_ENABLED"


def test_update_app_setting_validates_values(db):
    uid = db.make_operator()
    db.as_postgres()
    db.conn.execute("update users set role = 'admin' where id = %s", (uid,))
    db.as_operator(uid)
    for key, value in [
        ("allowed_emails", '["Not-Lower@Example.com"]'),
        ("allowed_emails", '"a@b.co"'),
        ("publishing_enabled", '"yes"'),
        ("limits", '{"unknown_limit": 1}'),
        ("limits", '{"daily_generation_limit": -1}'),
        ("retry_backoff_seconds", "[]"),
        ("heartbeat_timeout_seconds", "{}"),  # 이 RPC로는 못 바꾸는 키
    ]:
        err = db.expect_error("select update_app_setting(%s, %s::jsonb)", (key, value))
        assert err.sqlstate == "PT422", (key, value)


def test_resolve_system_error_is_owner_only(db):
    uid_a = db.make_operator()
    pid_a = db.make_persona(uid_a)
    uid_b = db.make_operator()
    db.as_postgres()
    err_id = db.val("insert into system_errors (persona_id, service, error_type, message)"
                    " values (%s, 'python', 'timeout', 'x') returning id", (pid_a,))
    db.as_operator(uid_b)
    assert db.expect_error("select resolve_system_error(%s)", (err_id,)).sqlstate == "PT404"
    db.as_operator(uid_a)
    assert db.one("select * from resolve_system_error(%s)", (err_id,))["resolved"] is True
    assert db.val("select get_dashboard_summary()")["recent_failures"] == []
