"""마이그레이션 적용 경로 (최종 게이트 10번): 신규 설치와, 데이터가 이미 있는 DB의 업그레이드.

  fresh   : 빈 DB에 스텁 + 0001~0011을 순서대로 (0006은 pg_cron이 없어 제외)
  upgrade : 0001~0009까지 적용하고 Job을 만든 DB에 0010 → 0011을 올린다

0010은 배포된 적이 없으므로 운영에서는 신규 설치만 일어난다. 그래도 업그레이드를 확인하는 이유는, 기존 행이 있을 때
새 컬럼·트리거·권한이 깨지지 않는다는 보장이 있어야 "다시 작성한 0010"을 믿을 수 있기 때문이다.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from tests.conftest import LOCAL_SKIP, MIGRATIONS, STUB, Db, _db_url
from tests.db.test_db import create_job, to_generating
from tests.db.test_worker_auth import claims, issue

ROOT = Path(__file__).resolve().parents[2]
BEFORE_0010 = [p for p in MIGRATIONS if p.name < "0010" and p.name not in LOCAL_SKIP]
MIGRATION_0010 = next(p for p in MIGRATIONS if p.name.startswith("0010_"))
MIGRATION_0011 = next(p for p in MIGRATIONS if p.name.startswith("0011_"))


@pytest.fixture
def blank_database(pg_admin_url):
    name = f"pa_up_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"create database {name}")
    url = _db_url(pg_admin_url, name)
    yield url
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


def apply(url, *paths):
    with psycopg.connect(url, autocommit=True) as conn:
        for path in paths:
            try:
                conn.execute(path.read_text(encoding="utf-8"))
            except psycopg.Error as exc:
                raise AssertionError(f"{path.name} failed: {exc}") from exc


def test_the_migration_files_are_numbered_without_gaps():
    numbers = [int(p.name[:4]) for p in MIGRATIONS]
    assert numbers == list(range(1, len(numbers) + 1)) and numbers[-1] == 14


def test_a_fresh_install_applies_every_migration_in_order(blank_database):
    apply(blank_database, STUB, *[p for p in MIGRATIONS if p.name not in LOCAL_SKIP])
    with psycopg.connect(blank_database) as conn:
        db = Db(conn)
        assert db.val("select value from app_settings where key = 'default_execution_target'") == "local"
        assert db.val("select count(*) from pg_roles where rolname = 'pa_worker'") == 1
        assert db.val("select count(*) from app_settings where key = 'active_worker'") == 0


def test_0011_requires_0010(blank_database):
    """0011은 0010이 만든 것(execution_target 컬럼, worker_target 함수)에 기댄다. 순서를 건너뛰면 실패해야 한다."""
    apply(blank_database, STUB, *BEFORE_0010)
    with pytest.raises(AssertionError, match="0011"):
        apply(blank_database, MIGRATION_0011)


def test_upgrading_a_database_that_already_has_jobs(blank_database):
    apply(blank_database, STUB, *BEFORE_0010)

    # 0009 상태의 DB에 Persona·Content Job·대기 중인 generation Job을 만든다
    with psycopg.connect(blank_database) as conn:
        db = Db(conn)
        uid = db.make_operator()
        pid = db.make_persona(uid)
        cj = to_generating(db, uid, pid)
        db.as_service("n8n")
        job = create_job(db, "generation", pid, cj["id"], key=f"generation:{cj['id']}:1")
        db.as_postgres()
        assert "execution_target" not in db.val(
            "select array_agg(column_name::text) from information_schema.columns"
            " where table_name = 'content_jobs'")                      # 아직 0010 이전이다
        conn.commit()

    apply(blank_database, MIGRATION_0010, MIGRATION_0011)             # 데이터가 있는 DB에 올린다

    with psycopg.connect(blank_database) as conn:
        db = Db(conn)
        db.as_postgres()
        # 기존 행은 local로 고정되고, automation_jobs는 상위를 따라간다 (null이 남지 않는다)
        assert db.val("select execution_target from content_jobs where id = %s", (cj["id"],)) == "local"
        assert db.val("select execution_target from automation_jobs where id = %s", (job["id"],)) == "local"
        assert db.val("select count(*) from automation_jobs where execution_target is null") == 0

        # 업그레이드 뒤에도 그 Job을 local 워커만 가져가고, cloud 워커는 못 가져간다
        db.conn.execute("update users set role = 'admin' where id = %s", (uid,))
        jti_cloud = issue(db, uid, worker_id="python:cloud-1", target="cloud", provider="runpod")
        jti_local = issue(db, uid, worker_id="python:local-1", target="local")
        db.as_worker(**claims(jti_cloud, worker_id="python:cloud-1", target="cloud"))
        assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],)) is None
        db.as_worker(**claims(jti_local))
        assert db.one("select * from claim_automation_job(%s, 'x')", (job["id"],))["claimed_by"] == "python:local-1"


def test_upgrade_keeps_the_applied_migrations_untouched(blank_database):
    """0001~0009를 적용한 DB에 0010·0011을 올려도 그 파일들은 읽기만 한다 (해시는 test_protected_files가 고정한다)."""
    before = {p.name: p.read_bytes() for p in BEFORE_0010}
    apply(blank_database, STUB, *BEFORE_0010, MIGRATION_0010, MIGRATION_0011)
    assert {p.name: p.read_bytes() for p in BEFORE_0010} == before
