"""공용 테스트 환경 (TECH_DESIGN 16.12).

pip로 설치하는 내장 PostgreSQL(pgserver)에 Supabase 흉내 스키마(supabase/tests/stubs)와
supabase/migrations를 적용한 **템플릿 DB**를 세션마다 한 번 만든다. 0006(pg_cron)은 로컬에 확장이 없어 건너뛴다.

* db (tests/db): 템플릿에서 만든 DB 하나를 공유하고, 테스트마다 트랜잭션을 롤백한다.
* fresh_database_url (tests/bridge): 테스트마다 템플릿을 복사한 새 DB. 커밋이 필요한 흐름에 쓴다.

역할 전환은 Supabase처럼 SET LOCAL ROLE + request.jwt.claims / request.headers로 흉내 낸다.

실행:  .venv/Scripts/python -m pytest tests -q
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterator

import pgserver
import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "supabase" / "tests" / "stubs" / "supabase_stub.sql"
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
LOCAL_SKIP = {"0006_cron.sql"}  # pg_cron 필요
TEMPLATE = "pa_template"


@pytest.fixture(scope="session")
def pg_admin_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """마이그레이션을 적용한 템플릿 DB를 만들고 관리자 URL을 돌려준다."""
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    admin_url = server.get_uri()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEMPLATE}")
        admin.execute(f"create database {TEMPLATE}")
    with psycopg.connect(_db_url(admin_url, TEMPLATE), autocommit=True) as conn:
        conn.execute(STUB.read_text(encoding="utf-8"))
        for path in MIGRATIONS:
            if path.name in LOCAL_SKIP:
                continue
            try:
                conn.execute(path.read_text(encoding="utf-8"))
            except psycopg.Error as exc:  # 어느 파일에서 실패했는지 보이게
                raise RuntimeError(f"migration {path.name} failed: {exc}") from exc
    yield admin_url


def _db_url(admin_url: str, name: str) -> str:
    return admin_url.rsplit("/", 1)[0] + "/" + name


def _create_from_template(admin_url: str, name: str) -> str:
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name}")
        admin.execute(f"create database {name} template {TEMPLATE}")
    return _db_url(admin_url, name)


@pytest.fixture(scope="session")
def database_url(pg_admin_url: str) -> str:
    return _create_from_template(pg_admin_url, "pa_test")


@pytest.fixture
def fresh_database_url(pg_admin_url: str) -> Iterator[str]:
    name = f"pa_{uuid.uuid4().hex[:12]}"
    url = _create_from_template(pg_admin_url, name)
    yield url
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f"drop database if exists {name} with (force)")


class Db:
    """한 테스트 트랜잭션 안에서 역할을 바꿔 가며 SQL을 실행하는 도우미."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # -- 역할 ---------------------------------------------------------------
    def as_postgres(self) -> "Db":
        self.conn.execute("reset role")
        self.conn.execute("select set_config('request.jwt.claims', '', true), set_config('request.headers', '', true)")
        return self

    def as_operator(self, uid: uuid.UUID | str) -> "Db":
        self.as_postgres()
        self.conn.execute("set local role authenticated")
        self.conn.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid), "role": "authenticated"}),),
        )
        return self

    def as_anon(self) -> "Db":
        self.as_postgres()
        self.conn.execute("set local role anon")
        self.conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"role": "anon"}),))
        return self

    def as_service(self, actor: str = "python") -> "Db":
        self.as_postgres()
        self.conn.execute("set local role service_role")
        self.conn.execute(
            "select set_config('request.headers', %s, true)", (json.dumps({"x-actor": actor}),)
        )
        return self

    # -- 실행 ---------------------------------------------------------------
    def one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def val(self, sql: str, params: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def expect_error(self, sql: str, params: Any = None) -> psycopg.Error:
        """SAVEPOINT 안에서 실행해 실패를 확인한다. 바깥 트랜잭션은 계속 쓸 수 있다."""
        try:
            with self.conn.transaction():
                self.conn.execute(sql, params)
        except psycopg.Error as exc:
            return exc
        raise AssertionError(f"expected an error from: {sql}")

    # -- 테스트 데이터 --------------------------------------------------------
    def make_operator(self, email: str | None = None) -> uuid.UUID:
        self.as_postgres()
        email = (email or f"op-{uuid.uuid4().hex[:8]}@example.com").lower()
        self.conn.execute(
            "update app_settings set value = value || to_jsonb(%s::text) where key = 'allowed_emails'", (email,)
        )
        return self.val(
            "insert into auth.users (email, raw_user_meta_data) values (%s, %s) returning id",
            (email, json.dumps({"full_name": "Test Operator"})),
        )

    def make_persona(self, uid: uuid.UUID, slug: str = "gina") -> uuid.UUID:
        self.as_operator(uid)
        return self.val(
            "insert into personas (name, slug, visual_settings) values (%s, %s, %s) returning id",
            ("Gina", slug, json.dumps({"default_workflow": "image_generation_v1"})),
        )

    def create_content_job(self, uid: uuid.UUID, persona_id: uuid.UUID, **kwargs: Any) -> dict[str, Any]:
        self.as_operator(uid)
        args = {"p_persona_id": persona_id, "p_content_type": "image", "p_topic": "도쿄 야경", **kwargs}
        names = ", ".join(f"{k} => %({k})s" for k in args)
        return self.one(f"select * from create_content_job({names})", args)


@pytest.fixture
def db(database_url: str) -> Iterator[Db]:
    with psycopg.connect(database_url) as conn:
        try:
            yield Db(conn)
        finally:
            conn.rollback()


def sqlstate(exc: psycopg.Error) -> str | None:
    return exc.sqlstate
