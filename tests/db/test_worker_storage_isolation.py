"""Storage 격리 (최종 보안 게이트, TECH_DESIGN 56.5): Worker는 자기가 선점한 Job의 파일만 다룬다.

Storage API는 요청마다 JWT의 DB 역할로 SQL을 실행하고 storage.objects의 RLS가 행을 거른다. 여기서는 그 SQL을
pa_worker 역할로 직접 실행해 정책(0011 8절)을 검증한다.

검증하지 못하는 것: 호스팅 Storage API가 role=pa_worker JWT를 받아 그 역할로 쿼리를 실행하는지 (외부 검증).

모델
  읽기: 선점한 Job이 입력으로 **참조한** 파일만 (경로를 추측해도, 같은 Persona의 다른 파일도, 목록 조회도 안 보인다)
  쓰기: media/persona/{선점한 Job의 Persona}/assets/{uuid}[_thumb].{png|jpg|webp} 뿐, 등록된 Asset 경로는 불가
  없음: UPDATE·DELETE 권한 (덮어쓰기·삭제 불가)
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.test_db import create_job, to_generating
from tests.db.test_worker_auth import admin, claims, issue  # noqa: F401

DENIED = "42501"


def uid() -> str:
    return str(uuid.uuid4())


def put_object(db, bucket, name):
    db.as_postgres()
    db.conn.execute("insert into storage.objects (bucket_id, name) values (%s, %s)", (bucket, name))


def make_asset_row(db, pid, content_job_id, asset_id, path):
    db.as_postgres()
    db.conn.execute(
        "insert into assets (id, persona_id, content_job_id, asset_type, file_name, storage_path, mime_type)"
        " values (%s, %s, %s, 'image', 'x.png', %s, 'image/png')", (asset_id, pid, content_job_id, path))
    put_object(db, "media", path)


def make_ref_row(db, pid, path):
    db.as_postgres()
    rid = uid()
    db.conn.execute("insert into persona_assets (id, persona_id, asset_type, name, storage_path)"
                    " values (%s, %s, 'face_ref', 'face', %s)", (rid, pid, path))
    put_object(db, "persona-private", path)
    return rid


class World:
    """Persona 하나, 그 Persona의 Job을 선점한 워커 W1, 입력으로 참조된 파일과 참조되지 않은 파일."""

    def __init__(self, db, admin_uid):
        self.db = db
        self.owner = admin_uid
        self.pid = db.make_persona(admin_uid)

        # 이 Persona의 기존 Asset: 하나는 입력으로 **참조**되고 하나는 참조되지 않는다
        seed_cj = db.create_content_job(admin_uid, self.pid)
        self.ref_asset, self.other_asset = uid(), uid()
        self.ref_asset_path = f"persona/{self.pid}/assets/{self.ref_asset}.png"
        self.other_asset_path = f"persona/{self.pid}/assets/{self.other_asset}.png"
        make_asset_row(db, self.pid, seed_cj["id"], self.ref_asset, self.ref_asset_path)
        make_asset_row(db, self.pid, seed_cj["id"], self.other_asset, self.other_asset_path)
        self.ref_persona_path = f"persona/{self.pid}/refs/face.png"
        self.unused_persona_path = f"persona/{self.pid}/refs/unused.png"
        self.ref_persona_asset = make_ref_row(db, self.pid, self.ref_persona_path)
        make_ref_row(db, self.pid, self.unused_persona_path)

        # 입력을 가진 Job을 W1이 선점한다
        self.cj = to_generating(db, admin_uid, self.pid, p_input_images=Jsonb({
            "init_image": {"asset_id": self.ref_asset},
            "face": {"persona_asset_id": self.ref_persona_asset}}))
        self.job = create_job(db, "generation", self.pid, self.cj["id"], key=f"generation:{self.cj['id']}:1")
        self.jti1 = issue(db, admin_uid, worker_id="python:local-1", target="local")
        db.as_worker(**claims(self.jti1, worker_id="python:local-1"))
        self.claimed = db.one("select * from claim_automation_job(%s, 'python:local-1')", (self.job["id"],))
        assert self.claimed["status"] == "processing"

        # 완전히 다른 Persona(다른 소유자): 파일 하나, 아직 선점되지 않은 Job 하나
        self.other_owner = db.make_operator()
        self.foreign_pid = db.make_persona(self.other_owner)
        foreign_cj = db.create_content_job(self.other_owner, self.foreign_pid)
        self.foreign_asset = uid()
        self.foreign_path = f"persona/{self.foreign_pid}/assets/{self.foreign_asset}.png"
        make_asset_row(db, self.foreign_pid, foreign_cj["id"], self.foreign_asset, self.foreign_path)

    def as_w1(self):
        return self.db.as_worker(**claims(self.jti1, worker_id="python:local-1"))

    def new_result_path(self, pid=None, suffix=".png", asset=None):
        return f"persona/{pid or self.pid}/assets/{asset or uid()}{suffix}"


@pytest.fixture
def w(db, admin):
    return World(db, admin)


def visible(db):
    """지금 역할이 storage.objects에서 볼 수 있는 (bucket, name) 전부 — RLS가 거른 결과."""
    return {(r[0], r[1]) for r in db.conn.execute("select bucket_id, name from storage.objects").fetchall()}


def insert_result(db, name, bucket="media"):
    db.conn.execute("insert into storage.objects (bucket_id, name) values (%s, %s)", (bucket, name))


# -----------------------------------------------------------------------------
# 읽기: 선점한 Job이 참조한 파일만
# -----------------------------------------------------------------------------
def test_worker_reads_only_the_files_its_claimed_job_references(db, w):
    w.as_w1()
    assert visible(db) == {("media", w.ref_asset_path), ("persona-private", w.ref_persona_path)}


def test_worker_cannot_enumerate_other_files_of_the_same_persona(db, w):
    w.as_w1()
    names = {n for _, n in visible(db)}
    assert w.other_asset_path not in names and w.unused_persona_path not in names


def test_worker_cannot_read_another_personas_files_even_with_a_guessed_path(db, w):
    w.as_w1()
    rows = db.conn.execute("select name from storage.objects where bucket_id = 'media' and name = %s",
                           (w.foreign_path,)).fetchall()
    assert rows == []


def test_a_worker_without_a_claim_sees_nothing(db, w):
    other = issue(db, w.owner, worker_id="python:local-2", target="local")
    db.as_worker(**claims(other, worker_id="python:local-2"))
    assert visible(db) == set()                       # 같은 Persona의 Job이 있어도 W1이 선점했다


def test_a_revoked_worker_sees_nothing(db, w):
    db.as_operator(w.owner)
    db.one("select * from revoke_worker_credential(%s)", (w.jti1,))
    w.as_w1()
    assert visible(db) == set()


def test_an_expired_worker_sees_nothing(db, w):
    db.as_postgres()
    db.conn.execute("update worker_credentials set issued_at = now() - interval '10 hours',"
                    " expires_at = now() - interval '1 hour' where jti = %s", (w.jti1,))
    w.as_w1()
    assert visible(db) == set()


def test_access_ends_when_the_job_is_no_longer_claimed(db, w):
    """TOCTOU: Job이 끝나면(또는 회수되면) 입력·출력 파일 접근도 같이 끝난다."""
    w.as_w1()
    db.val("select fail_automation_job(%s, %s, 'validation', 'X', 'done with it', false)",
           (w.job["id"], w.claimed["locked_at"]))
    w.as_w1()
    assert visible(db) == set()
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)",
                           (w.new_result_path(),)).sqlstate == DENIED


# -----------------------------------------------------------------------------
# 쓰기: 올바른 경로만
# -----------------------------------------------------------------------------
def test_worker_writes_a_result_and_its_thumbnail_for_the_claimed_persona(db, w):
    w.as_w1()
    asset = uid()
    insert_result(db, w.new_result_path(asset=asset))
    insert_result(db, w.new_result_path(asset=asset, suffix="_thumb.webp"))
    insert_result(db, w.new_result_path(suffix=".jpg"))


@pytest.mark.parametrize(
    "build",
    [
        lambda w: w.new_result_path(pid=w.foreign_pid),                      # 다른 Persona
        lambda w: f"persona/{uid()}/assets/{uid()}.png",                     # 존재하지 않는 Persona
        lambda w: f"persona/{w.pid}/refs/{uid()}.png",                       # assets가 아닌 폴더
        lambda w: f"persona/{w.pid}/assets/../{uid()}.png",                  # 경로 조작
        lambda w: f"persona/{w.pid}/assets/{uid()}/../{uid()}.png",
        lambda w: f"persona/{w.pid}/assets/sub/{uid()}.png",                 # 중첩
        lambda w: f"persona/{w.pid}/assets/{uid()}.svg",                     # 허용되지 않는 확장자
        lambda w: f"persona/{w.pid}/assets/{uid()}.exe",
        lambda w: f"persona/{w.pid}/assets/{uid().upper()}.png",             # 대문자 UUID
        lambda w: f"persona/{str(w.pid).upper()}/assets/{uid()}.png",
        lambda w: f"persona/{w.pid}/assets/not-a-uuid.png",
        lambda w: f"/persona/{w.pid}/assets/{uid()}.png",                    # 절대 경로
        lambda w: f"persona/{w.pid}/assets/{uid()}.png\n",                   # 개행
        lambda w: f"other/{w.pid}/assets/{uid()}.png",
        lambda w: "anything.png",
    ],
)
def test_worker_cannot_write_to_a_wrong_or_arbitrary_path(db, w, build):
    w.as_w1()
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)",
                           (build(w),)).sqlstate == DENIED


def test_worker_can_write_only_to_the_media_bucket(db, w):
    w.as_w1()
    for bucket in ("persona-private", "generated-assets", "nope"):
        assert db.expect_error("insert into storage.objects (bucket_id, name) values (%s, %s)",
                               (bucket, w.new_result_path())).sqlstate == DENIED, bucket


def test_worker_cannot_write_for_a_persona_whose_job_belongs_to_someone_else(db, w, admin):
    """다른 워커가 선점한 Job의 Persona 경로에는 쓸 수 없다."""
    pid2 = w.foreign_pid
    cj2 = to_generating(db, w.other_owner, pid2)
    job2 = create_job(db, "generation", pid2, cj2["id"], key=f"generation:{cj2['id']}:1")
    jti2 = issue(db, admin, worker_id="python:local-2", target="local")
    db.as_worker(**claims(jti2, worker_id="python:local-2"))
    assert db.one("select * from claim_automation_job(%s, 'python:local-2')", (job2["id"],)) is not None

    w.as_w1()                                                           # W1은 이 Persona의 Job이 없다
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)",
                           (w.new_result_path(pid=pid2),)).sqlstate == DENIED
    db.as_worker(**claims(jti2, worker_id="python:local-2"))            # W2는 쓸 수 있다
    insert_result(db, w.new_result_path(pid=pid2))


def test_worker_cannot_write_for_a_persona_with_only_a_pending_job(db, w):
    owner3 = db.make_operator()
    pid3 = db.make_persona(owner3)
    cj3 = to_generating(db, owner3, pid3)
    create_job(db, "generation", pid3, cj3["id"], key=f"generation:{cj3['id']}:1")   # 선점되지 않았다
    w.as_w1()
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)",
                           (w.new_result_path(pid=pid3),)).sqlstate == DENIED


# -----------------------------------------------------------------------------
# 덮어쓰기·삭제·변경 불가
# -----------------------------------------------------------------------------
def test_worker_cannot_overwrite_a_registered_asset_path(db, w):
    w.as_w1()
    assert db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)",
                           (w.ref_asset_path,)).sqlstate == DENIED


def test_worker_cannot_overwrite_an_existing_unregistered_object(db, w):
    """등록되지 않은 기존 객체: 정책은 통과해도 (bucket_id, name) 유일 제약이 덮어쓰기를 막는다."""
    path = w.new_result_path()
    put_object(db, "media", path)
    w.as_w1()
    err = db.expect_error("insert into storage.objects (bucket_id, name) values ('media', %s)", (path,))
    assert isinstance(err, psycopg.errors.UniqueViolation)


@pytest.mark.parametrize(
    "sql",
    [
        "update storage.objects set name = name",
        "update storage.objects set metadata = '{}'::jsonb",
        "delete from storage.objects",
        "truncate storage.objects",
        "select * from storage.buckets",
        "insert into storage.buckets (id, name) values ('evil', 'evil')",
    ],
)
def test_worker_has_no_update_delete_or_bucket_access(db, w, sql):
    w.as_w1()
    assert db.expect_error(sql).sqlstate == DENIED, sql


def test_a_failed_attempt_leaves_every_existing_object_untouched(db, w):
    before = db.conn.execute("select count(*) from storage.objects").fetchone()[0]
    w.as_w1()
    for sql in ("delete from storage.objects", "update storage.objects set name = name || 'x'"):
        db.expect_error(sql)
    db.as_postgres()
    assert db.conn.execute("select count(*) from storage.objects").fetchone()[0] == before


# -----------------------------------------------------------------------------
# DB 쪽 경계: 입력 Asset 조회와 Asset 등록 (TOCTOU)
# -----------------------------------------------------------------------------
def test_input_asset_rpc_returns_only_referenced_assets(db, w):
    w.as_w1()
    got = db.val("select worker_get_input_asset(%s, %s)", (w.job["id"], w.ref_asset))
    assert got["storage_path"] == w.ref_asset_path
    for unreferenced in (w.other_asset, w.foreign_asset, uid()):
        w.as_w1()
        assert db.val("select worker_get_input_asset(%s, %s)", (w.job["id"], unreferenced)) is None


def test_job_context_lists_only_referenced_input_assets(db, w):
    w.as_w1()
    ctx = db.val("select worker_get_job_context(%s)", (w.job["id"],))
    assert [a["id"] for a in ctx["input_assets"]] == [w.ref_asset]
    assert w.other_asset not in str(ctx) and w.foreign_asset not in str(ctx)


def test_context_for_job_a_cannot_be_used_to_register_an_asset_against_job_b(db, w, admin):
    """같은 Persona의 Job B를 다른 워커가 쥐고 있으면, W1은 B에 Asset을 등록하지 못한다."""
    cj_b = to_generating(db, admin, w.pid)
    job_b = create_job(db, "generation", w.pid, cj_b["id"], key=f"generation:{cj_b['id']}:1")
    jti2 = issue(db, admin, worker_id="python:local-2", target="local")
    db.as_worker(**claims(jti2, worker_id="python:local-2"))
    claimed_b = db.one("select * from claim_automation_job(%s, 'python:local-2')", (job_b["id"],))

    asset = uid()
    payload = {"id": asset, "asset_type": "image", "file_name": "x.png", "mime_type": "image/png",
               "storage_path": f"persona/{w.pid}/assets/{asset}.png"}
    w.as_w1()
    # W1이 B의 locked_at을 알아도(최악) B에는 등록할 수 없다
    assert db.one("select * from register_asset(%s, %s, %s)",
                  (job_b["id"], claimed_b["locked_at"], Jsonb(payload))) is None
    db.as_postgres()
    assert db.conn.execute("select count(*) from assets where id = %s", (asset,)).fetchone()[0] == 0


def test_an_asset_can_only_be_registered_under_the_claimed_jobs_persona_path(db, w):
    asset = uid()
    w.as_w1()
    bad = {"id": asset, "asset_type": "image", "file_name": "x.png", "mime_type": "image/png",
           "storage_path": f"persona/{w.foreign_pid}/assets/{asset}.png"}
    assert db.expect_error("select * from register_asset(%s, %s, %s)",
                           (w.job["id"], w.claimed["locked_at"], Jsonb(bad))).sqlstate == "PT422"
    good = {**bad, "storage_path": f"persona/{w.pid}/assets/{asset}.png"}
    w.as_w1()
    assert db.one("select * from register_asset(%s, %s, %s)",
                  (w.job["id"], w.claimed["locked_at"], Jsonb(good)))["storage_path"] == good["storage_path"]
