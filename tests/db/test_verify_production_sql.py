"""supabase/verify_production.sql의 0010·0011 점검 블록(20~23)을 로컬 DB에서 **실제로 실행**해 기대값과 맞는지 본다.

호스팅 프로젝트에서 사람이 돌리는 SQL이 문법 오류나 틀린 기대 주석을 품은 채로 남지 않게 한다.
블록 1~19의 일부는 pg_cron·pg_net·supabase_migrations가 필요해 로컬에서 실행할 수 없다 (not-runnable).
22번(authenticator 멤버십)은 로컬 스텁에 authenticator 역할이 없어 0행이다 — 호스팅에서만 1행이 나온다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "supabase" / "verify_production.sql").read_text(encoding="utf-8")


def block(number: int) -> list[str]:
    """'-- N. ' 제목부터 다음 번호 제목 앞까지의 SQL 문장들 (주석 제외)."""
    match = re.search(rf"^-- {number}\. .*?(?=^-- {number + 1}\. |\Z)", SQL, re.S | re.M)
    assert match, f"block {number} not found"
    text = "\n".join(line for line in match.group(0).splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in text.split(";") if s.strip()]


def run(db, number: int) -> list[list[tuple]]:
    db.as_postgres()
    return [db.conn.execute(stmt).fetchall() for stmt in block(number)]


@pytest.mark.parametrize("number", [20, 21, 22, 23, 25, 26])
def test_every_check_block_runs_on_a_fresh_database(db, number):
    results = run(db, number)
    assert results                                   # 문장이 하나 이상 있고 모두 오류 없이 실행된다


def test_block_24_is_guidance_not_a_database_check():
    assert block(24) == []


def test_block_20_execution_target_expectations(db):
    settings, columns, leaks, trigger, rpcs = run(db, 20)
    assert dict(settings) == {"app_mode": "personal", "default_execution_target": "local"}   # active_worker 행 없음
    assert {(t, c) for t, c, _ in columns} == {("content_jobs", "execution_target"), ("automation_jobs", "execution_target"),
                                               ("worker_status", "target"), ("worker_status", "provider")}
    assert {(t, c): n for t, c, n in columns}[("content_jobs", "execution_target")] == "NO"  # not null
    assert leaks == []                               # authenticated는 execution_target을 INSERT·UPDATE할 수 없다
    assert [r[0] for r in trigger] == ["automation_jobs_inherit_execution_target"]
    assert {(n, a) for n, a in rpcs} == {("create_content_job", 13), ("move_pending_jobs_to_target", 1),
                                         ("retry_automation_job", 2)}


def test_block_21_worker_auth_expectations(db):
    rls, secret_like, role, grants, public_fns, private_fns, flags = run(db, 21)
    assert rls == [("worker_credentials", True, 0)]                  # RLS 켜짐, 정책 0개
    assert secret_like == [(0,)]                                     # 서명 키 열 없음
    assert role == [("pa_worker", False)]                            # NOLOGIN
    assert grants == [(0,)]                                          # public 테이블 권한 0건
    assert len(public_fns) == 12                                     # 정확히 12개
    assert {r[0] for r in private_fns} == {"worker_may_read_object", "worker_may_write_object"}
    assert flags == [(False, False, False, False, False)]           # authenticated·anon은 워커 RPC 실행 불가


def test_block_22_authenticator_membership_is_hosted_only(db):
    (members,) = run(db, 22)
    assert members == []                              # 로컬 스텁에는 authenticator가 없다. 호스팅에서는 1행이어야 한다


def test_block_23_storage_isolation_expectations(db):
    grants, policies, forbidden_policies, rls = run(db, 23)
    assert grants == [("storage", "objects", "INSERT"), ("storage", "objects", "SELECT")]
    assert [(p, c) for p, c, _ in policies] == [("pa_worker_read_inputs", "SELECT"), ("pa_worker_write_results", "INSERT")]
    assert forbidden_policies == []                   # UPDATE·DELETE·ALL 정책 없음 → 덮어쓰기·삭제 불가
    assert rls == [("objects", True)]


def test_block_25_reports_the_review_asset_surface(db):
    """0013: review_note 칸이 있고, authenticated만 review_asset을 실행할 수 있다."""
    assert run(db, 25) == [[(True, True, False)]]


def test_block_26_reports_the_emergency_stop_surface_and_the_switch_state(db):
    """0014: 생성 스위치가 있고, authenticated만 emergency_stop_all을 실행할 수 있다. 평소에는 생성 켜짐·게시 꺼짐."""
    first, switch_state = run(db, 26)
    assert first == [(True, True, False)]
    assert switch_state == [("generation_enabled", True), ("publishing_enabled", False)]
