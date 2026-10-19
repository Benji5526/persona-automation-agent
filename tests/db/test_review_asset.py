"""0013 Asset 검토: review_asset (TECH_DESIGN 12.4, 50.5 3번, MVP 완료 목록 10번).

* 승인·반려 전이와 반려 사유(review_note) 저장·초기화, 누가·언제는 state_transitions
* 잘못된 입력(결정, 사유 길이, 승인에 사유)과 허용되지 않는 전이
* 게시·예약·게시 중인 Post가 있는 Asset은 반려할 수 없다 (0002 가드)
* 교차 계정(PT404), 직접 쓰기 금지, anon 실행 금지
"""

from __future__ import annotations

import pytest

from tests.db.test_db import claim, create_job, to_ready

PERMISSION_DENIED = "42501"


@pytest.fixture
def op(db):
    uid = db.make_operator()
    return uid, db.make_persona(uid)


@pytest.fixture
def two_operators(db):
    uid_a = db.make_operator()
    pid_a = db.make_persona(uid_a, slug="gina")
    uid_b = db.make_operator()
    pid_b = db.make_persona(uid_b, slug="mina")
    return uid_a, pid_a, uid_b, pid_b


def review(db, uid, asset_id, decision, note=None):
    db.as_operator(uid)
    return db.one("select * from review_asset(%s, %s, %s)", (asset_id, decision, note))


def asset_row(db, asset_id):
    db.as_postgres()
    return db.one("select status, review_note from assets where id = %s", (asset_id,))


def transitions(db, asset_id):
    db.as_postgres()
    return db.all("select from_status, to_status, actor_type, actor_id, reason from state_transitions"
                  " where entity_type = 'asset' and entity_id = %s order by id", (asset_id,))


def ready_asset(db, uid, pid):
    cj, _, asset = to_ready(db, uid, pid)
    return cj, asset


# -----------------------------------------------------------------------------
# 전이와 사유
# -----------------------------------------------------------------------------
def test_approve_a_generated_asset_and_log_who_did_it(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)

    result = review(db, uid, asset["id"], "approved")
    assert result["status"] == "approved" and result["review_note"] is None

    last = transitions(db, asset["id"])[-1]
    assert (last["from_status"], last["to_status"]) == ("generated", "approved")
    assert last["actor_type"] == "operator" and last["actor_id"] == uid       # 누가
    assert last["reason"] == "operator:approve"


def test_reject_saves_the_trimmed_note_and_logs_it(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)

    result = review(db, uid, asset["id"], "rejected", "  손가락이 어색해요  ")
    assert result["status"] == "rejected" and result["review_note"] == "손가락이 어색해요"

    last = transitions(db, asset["id"])[-1]
    assert (last["from_status"], last["to_status"]) == ("generated", "rejected")
    assert last["actor_id"] == uid and last["reason"] == "operator:reject"


@pytest.mark.parametrize("note", [None, "", "   "])
def test_reject_without_a_note_is_allowed_and_stores_null(db, op, note):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    assert review(db, uid, asset["id"], "rejected", note)["review_note"] is None


def test_a_note_of_exactly_500_characters_is_accepted(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    assert len(review(db, uid, asset["id"], "rejected", "가" * 500)["review_note"]) == 500


def test_approving_again_clears_the_reject_note(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    review(db, uid, asset["id"], "rejected", "구도가 어색해요")
    assert asset_row(db, asset["id"])["review_note"] == "구도가 어색해요"

    result = review(db, uid, asset["id"], "approved")
    assert result["status"] == "approved" and result["review_note"] is None
    assert asset_row(db, asset["id"]) == {"status": "approved", "review_note": None}


def test_a_decision_can_be_changed_in_both_directions(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    assert review(db, uid, asset["id"], "approved")["status"] == "approved"
    assert review(db, uid, asset["id"], "rejected", "마음이 바뀌었어요")["status"] == "rejected"
    assert review(db, uid, asset["id"], "approved")["status"] == "approved"
    path = [(t["from_status"], t["to_status"]) for t in transitions(db, asset["id"])]
    assert path[-3:] == [("generated", "approved"), ("approved", "rejected"), ("rejected", "approved")]


def test_the_same_decision_twice_is_an_invalid_transition(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    review(db, uid, asset["id"], "approved")
    assert db.expect_error("select * from review_asset(%s, 'approved')", (asset["id"],)).sqlstate == "PT409"

    review(db, uid, asset["id"], "rejected", "no")
    assert db.expect_error("select * from review_asset(%s, 'rejected', 'again')", (asset["id"],)).sqlstate == "PT409"
    assert asset_row(db, asset["id"]) == {"status": "rejected", "review_note": "no"}   # 두 번째 호출은 아무것도 바꾸지 않는다


def test_an_archived_asset_cannot_be_reviewed(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    db.one("select * from archive_asset(%s)", (asset["id"],))
    for decision in ("approved", "rejected"):
        assert db.expect_error("select * from review_asset(%s, %s)", (asset["id"], decision)).sqlstate == "PT409"
    assert asset_row(db, asset["id"])["status"] == "archived"


# -----------------------------------------------------------------------------
# 입력 검증
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("decision", ["archived", "generated", "APPROVED", "", None])
def test_only_approved_or_rejected_is_a_valid_decision(db, op, decision):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("select * from review_asset(%s, %s)", (asset["id"], decision)).sqlstate == "PT422"
    assert asset_row(db, asset["id"]) == {"status": "generated", "review_note": None}


def test_a_note_longer_than_500_characters_is_refused(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("select * from review_asset(%s, 'rejected', %s)", (asset["id"], "가" * 501)).sqlstate == "PT422"
    assert asset_row(db, asset["id"]) == {"status": "generated", "review_note": None}


def test_a_note_is_only_for_rejection(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("select * from review_asset(%s, 'approved', 'good')", (asset["id"],)).sqlstate == "PT422"
    assert asset_row(db, asset["id"])["status"] == "generated"


def test_the_column_itself_also_limits_the_note_to_500_characters(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_postgres()
    assert db.expect_error("update assets set review_note = repeat('x', 501) where id = %s",
                           (asset["id"],)).sqlstate == "23514"


# -----------------------------------------------------------------------------
# 게시 가드 (0002): 게시·예약·게시 중인 Post가 있는 승인된 Asset은 반려할 수 없다
# -----------------------------------------------------------------------------
def test_an_approved_asset_with_a_publishing_post_cannot_be_rejected(db, op):
    uid, pid = op
    cj, asset = ready_asset(db, uid, pid)
    review(db, uid, asset["id"], "approved")

    db.as_service("n8n")
    cjob = create_job(db, "caption", pid, cj["id"], key=f"caption:{asset['id']}")
    c = claim(db, cjob["id"], "n8n")
    post = db.one("select * from create_post_draft(%s, %s, %s, 'instagram', %s, array['tokyo'])",
                  (c["id"], c["locked_at"], asset["id"], "도쿄의 밤"))
    assert db.val("select complete_automation_job(%s, %s)", (c["id"], c["locked_at"])) is True

    db.as_postgres()
    acct = db.val("insert into social_accounts (persona_id, platform, account_id, status)"
                  " values (%s, 'instagram', 'ig-review', 'active') returning id", (pid,))
    db.conn.execute("update posts set social_account_id = %s, status = 'pending_approval' where id = %s", (acct, post["id"]))
    db.conn.execute("insert into approvals (user_id, persona_id, post_id) values (%s, %s, %s)", (uid, pid, post["id"]))
    db.conn.execute("update approvals set status = 'approved', resolved_at = now() where post_id = %s", (post["id"],))
    db.conn.execute("update posts set status = 'publishing' where id = %s", (post["id"],))

    db.as_operator(uid)
    assert db.expect_error("select * from review_asset(%s, 'rejected', 'late')", (asset["id"],)).sqlstate == "PT409"
    assert asset_row(db, asset["id"]) == {"status": "approved", "review_note": None}


def test_reviewing_an_asset_does_not_change_its_content_job(db, op):
    uid, pid = op
    cj, asset = ready_asset(db, uid, pid)
    review(db, uid, asset["id"], "rejected", "다시 만들어 주세요")
    db.as_postgres()
    assert db.val("select status from content_jobs where id = %s", (cj["id"],)) == "ready"


# -----------------------------------------------------------------------------
# 격리와 권한
# -----------------------------------------------------------------------------
def test_another_operator_cannot_review_my_asset(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    _, asset = ready_asset(db, uid_a, pid_a)

    db.as_operator(uid_b)
    assert db.expect_error("select * from review_asset(%s, 'approved')", (asset["id"],)).sqlstate == "PT404"
    assert db.expect_error("select * from review_asset(%s, 'rejected', 'x')", (asset["id"],)).sqlstate == "PT404"
    assert asset_row(db, asset["id"]) == {"status": "generated", "review_note": None}


def test_the_owner_cannot_write_review_columns_directly(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("update assets set review_note = 'x' where id = %s", (asset["id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("update assets set status = 'approved' where id = %s", (asset["id"],)).sqlstate == PERMISSION_DENIED
    assert asset_row(db, asset["id"]) == {"status": "generated", "review_note": None}


def test_anon_cannot_execute_review_asset(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    db.as_anon()
    assert db.expect_error("select * from review_asset(%s, 'approved')", (asset["id"],)).sqlstate == PERMISSION_DENIED
    assert asset_row(db, asset["id"])["status"] == "generated"


def test_the_reviewer_can_read_the_note_on_their_own_asset(db, op):
    uid, pid = op
    _, asset = ready_asset(db, uid, pid)
    review(db, uid, asset["id"], "rejected", "배경이 흐려요")
    db.as_operator(uid)
    assert db.val("select review_note from assets where id = %s", (asset["id"],)) == "배경이 흐려요"
