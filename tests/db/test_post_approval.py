"""0015 Post 승인 흐름 (TECH_DESIGN 11.8~11.10, 12.4, 28.7, 33.7, MVP 완료 목록 11번).

* create_post_from_asset: Asset → 캡션 초안(draft). 입력 검증, 게시할 수 없는 Asset·남의 Asset·잘못된 계정 거부
* submit_post_for_approval: draft → pending_approval + 대기 중인 approvals 행
* resolve_approval: 승인·반려 → (롤업 R6) Post 반영, 누가·언제는 state_transitions
* revise_post(rejected → draft), cancel_post(R5: 대기 승인·진행 중 Job 취소, publishing 중에는 거부)
* 승인 없이 approved가 될 수 없다 (service_role로도), 새 테이블 approvals의 교차 계정 격리·직접 쓰기 금지
"""

from __future__ import annotations

import uuid

import pytest

from tests.db.test_db import to_ready

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


def ready_asset(db, uid, pid):
    return to_ready(db, uid, pid)[2]


def account(db, pid, platform="instagram", status="active"):
    db.as_postgres()
    return db.val("insert into social_accounts (persona_id, platform, account_id, status) values (%s, %s, %s, %s)"
                  " returning id", (pid, platform, f"acc-{uuid.uuid4().hex[:8]}", status))


def create(db, uid, asset_id, caption="도쿄의 밤", platform="instagram", hashtags=None, account_id=None, sponsored=False):
    db.as_operator(uid)
    return db.one("select * from create_post_from_asset(%s, %s, %s, %s, %s, %s)",
                  (asset_id, platform, caption, hashtags or [], account_id, sponsored))


def submit(db, uid, post_id, account_id=None):
    db.as_operator(uid)
    return db.one("select * from submit_post_for_approval(%s, %s)", (post_id, account_id))


def resolve(db, uid, approval_id, decision, comment=None):
    db.as_operator(uid)
    return db.one("select * from resolve_approval(%s, %s, %s)", (approval_id, decision, comment))


def pending_post(db, uid, pid):
    """승인 대기 중인 Post와 그 승인 행."""
    asset = ready_asset(db, uid, pid)
    acc = account(db, pid)
    post = create(db, uid, asset["id"], account_id=acc)
    submit(db, uid, post["id"])
    return post, approval_of(db, post["id"]), asset, acc


def approval_of(db, post_id, status="pending"):
    db.as_postgres()
    return db.one("select * from approvals where post_id = %s and status = %s", (post_id, status))


def post_row(db, post_id):
    db.as_postgres()
    return db.one("select * from posts where id = %s", (post_id,))


def transitions(db, entity, entity_id):
    db.as_postgres()
    return db.all("select from_status, to_status, actor_type, actor_id, reason from state_transitions"
                  " where entity_type = %s and entity_id = %s order by id", (entity, entity_id))


# -----------------------------------------------------------------------------
# create_post_from_asset
# -----------------------------------------------------------------------------
def test_creating_a_post_makes_a_draft_with_cleaned_caption_and_hashtags(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)

    post = create(db, uid, asset["id"], caption="  도쿄의 밤  ", hashtags=["#tokyo", " night ", "tokyo", "", "##neon"])

    assert post["status"] == "draft" and post["platform"] == "instagram" and post["persona_id"] == pid
    assert post["caption"] == "도쿄의 밤"
    assert post["hashtags"] == ["tokyo", "night", "neon"]            # '#' 제거, 공백 제거, 중복·빈 값 버림
    assert post["social_account_id"] is None and post["is_sponsored"] is False
    [t] = transitions(db, "post", post["id"])
    assert (t["from_status"], t["to_status"], t["actor_type"], t["actor_id"]) == (None, "draft", "operator", uid)


def test_an_approved_asset_and_a_generated_asset_can_both_get_a_post(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    assert create(db, uid, asset["id"])["status"] == "draft"          # generated
    db.as_operator(uid)
    db.val("select review_asset(%s, 'approved')", (asset["id"],))
    assert create(db, uid, asset["id"], platform="x")["status"] == "draft"   # approved, 같은 Asset의 다른 플랫폼


@pytest.mark.parametrize("final", ["rejected", "archived"])
def test_a_rejected_or_archived_asset_cannot_get_a_post(db, op, final):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    if final == "rejected":
        db.val("select review_asset(%s, 'rejected', 'blurry')", (asset["id"],))
    else:
        db.val("select archive_asset(%s)", (asset["id"],))
    err = db.expect_error("select * from create_post_from_asset(%s, 'instagram', 'hi')", (asset["id"],))
    assert err.sqlstate == "PT422"


@pytest.mark.parametrize("kwargs", [
    {"platform": "tiktok"},                                          # CHECK에는 있지만 MVP는 지원하지 않는다
    {"platform": "facebook"},
    {"caption": "   "},
    {"caption": "가" * 2201},
    {"hashtags": [f"t{i}" for i in range(31)]},
    {"hashtags": ["two words"]},
    {"hashtags": ["a#b"]},
])
def test_invalid_input_is_rejected_with_a_validation_error(db, op, kwargs):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    db.as_operator(uid)
    args = {"platform": "instagram", "caption": "hi", "hashtags": [], **kwargs}
    err = db.expect_error("select * from create_post_from_asset(%s, %s, %s, %s)",
                          (asset["id"], args["platform"], args["caption"], args["hashtags"]))
    assert err.sqlstate == "PT422"


def test_exactly_2200_characters_and_30_hashtags_are_fine(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    post = create(db, uid, asset["id"], caption="가" * 2200, hashtags=[f"t{i}" for i in range(30)])
    assert len(post["caption"]) == 2200 and len(post["hashtags"]) == 30


def test_another_operators_asset_looks_like_it_does_not_exist(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    asset = ready_asset(db, uid_a, pid_a)
    db.as_operator(uid_b)
    err = db.expect_error("select * from create_post_from_asset(%s, 'instagram', 'hi')", (asset["id"],))
    assert err.sqlstate == "PT404"
    assert post_count(db) == 0


def post_count(db):
    db.as_postgres()
    return db.val("select count(*) from posts")


def test_an_account_of_another_persona_or_platform_or_state_is_rejected(db, two_operators):
    uid_a, pid_a, uid_b, pid_b = two_operators
    asset = ready_asset(db, uid_a, pid_a)
    for bad in (account(db, pid_b),                                  # 남의 계정
                account(db, pid_a, platform="x"),                    # 플랫폼이 다름
                account(db, pid_a, status="inactive")):              # 연결 해제됨
        db.as_operator(uid_a)
        err = db.expect_error("select * from create_post_from_asset(%s, 'instagram', 'hi', '{}', %s)",
                              (asset["id"], bad))
        assert err.sqlstate == "PT422"
    assert post_count(db) == 0


def test_an_inactive_persona_cannot_get_new_posts(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    db.as_postgres()
    db.conn.execute("update personas set status = 'inactive' where id = %s", (pid,))
    db.as_operator(uid)
    err = db.expect_error("select * from create_post_from_asset(%s, 'instagram', 'hi')", (asset["id"],))
    assert err.sqlstate == "PT422"


def test_anon_cannot_create_posts(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    db.as_anon()
    assert db.expect_error("select * from create_post_from_asset(%s, 'instagram', 'hi')",
                           (asset["id"],)).sqlstate == PERMISSION_DENIED


# -----------------------------------------------------------------------------
# submit_post_for_approval
# -----------------------------------------------------------------------------
def test_submitting_moves_the_post_to_pending_and_creates_one_pending_approval(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    acc = account(db, pid)
    post = create(db, uid, asset["id"])

    result = submit(db, uid, post["id"], acc)

    assert result["status"] == "pending_approval" and result["social_account_id"] == acc
    approval = approval_of(db, post["id"])
    assert approval["status"] == "pending" and approval["user_id"] == uid and approval["persona_id"] == pid
    assert approval["approval_type"] == "publish" and approval["resolved_at"] is None
    assert [t["to_status"] for t in transitions(db, "post", post["id"])] == ["draft", "pending_approval"]
    assert [(t["from_status"], t["to_status"]) for t in transitions(db, "approval", approval["id"])] == [(None, "pending")]


def test_the_account_chosen_at_creation_is_used_when_none_is_given_at_submit(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    acc = account(db, pid)
    post = create(db, uid, asset["id"], account_id=acc)
    assert submit(db, uid, post["id"])["social_account_id"] == acc


def test_submit_needs_a_usable_account(db, two_operators):
    uid_a, pid_a, uid_b, pid_b = two_operators
    asset = ready_asset(db, uid_a, pid_a)
    post = create(db, uid_a, asset["id"])
    cases = {
        "none": None,
        "another persona": account(db, pid_b),
        "wrong platform": account(db, pid_a, platform="x"),
        "inactive": account(db, pid_a, status="inactive"),
    }
    for label, acc in cases.items():
        db.as_operator(uid_a)
        err = db.expect_error("select * from submit_post_for_approval(%s, %s)", (post["id"], acc))
        assert err.sqlstate == "PT422", label
    assert post_row(db, post["id"])["status"] == "draft"
    assert approval_of(db, post["id"]) is None


def test_submit_twice_does_not_create_a_second_approval(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("select * from submit_post_for_approval(%s)", (post["id"],)).sqlstate == "PT409"
    db.as_postgres()
    assert db.val("select count(*) from approvals where post_id = %s", (post["id"],)) == 1


def test_submit_of_another_operators_post_is_not_found(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    asset = ready_asset(db, uid_a, pid_a)
    post = create(db, uid_a, asset["id"], account_id=account(db, pid_a))
    db.as_operator(uid_b)
    assert db.expect_error("select * from submit_post_for_approval(%s)", (post["id"],)).sqlstate == "PT404"


def test_submit_refuses_a_post_whose_asset_was_archived_meanwhile(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    post = create(db, uid, asset["id"], account_id=account(db, pid))
    db.as_operator(uid)
    db.val("select archive_asset(%s)", (asset["id"],))
    assert db.expect_error("select * from submit_post_for_approval(%s)", (post["id"],)).sqlstate == "PT422"


# -----------------------------------------------------------------------------
# resolve_approval
# -----------------------------------------------------------------------------
def test_approving_approves_the_post_and_logs_who_and_when(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)

    result = resolve(db, uid, approval["id"], "approved", "  좋아요  ")

    assert result["status"] == "approved" and result["comment"] == "좋아요" and result["resolved_at"] is not None
    assert post_row(db, post["id"])["status"] == "approved"
    last = transitions(db, "approval", approval["id"])[-1]
    assert (last["from_status"], last["to_status"], last["actor_type"], last["actor_id"]) == \
           ("pending", "approved", "operator", uid)
    assert last["reason"] == "operator:approve_post"
    assert transitions(db, "post", post["id"])[-1]["to_status"] == "approved"


def test_rejecting_rejects_the_post_and_keeps_the_comment(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)

    result = resolve(db, uid, approval["id"], "rejected", "캡션이 어색해요")

    assert result["status"] == "rejected" and result["comment"] == "캡션이 어색해요"
    assert post_row(db, post["id"])["status"] == "rejected"


def test_an_approval_can_be_resolved_only_once(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "approved")
    db.as_operator(uid)
    assert db.expect_error("select * from resolve_approval(%s, 'rejected')", (approval["id"],)).sqlstate == "PT409"
    assert post_row(db, post["id"])["status"] == "approved"


def test_resolve_input_checks(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("select * from resolve_approval(%s, 'maybe')", (approval["id"],)).sqlstate == "PT422"
    assert db.expect_error("select * from resolve_approval(%s, 'approved', %s)",
                           (approval["id"], "가" * 501)).sqlstate == "PT422"
    assert db.expect_error("select * from resolve_approval(%s, 'approved')", (uuid.uuid4(),)).sqlstate == "PT404"
    assert approval_of(db, post["id"])["status"] == "pending"


def test_another_operator_cannot_resolve_my_approval(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    post, approval, _, _ = pending_post(db, uid_a, pid_a)
    db.as_operator(uid_b)
    assert db.expect_error("select * from resolve_approval(%s, 'approved')", (approval["id"],)).sqlstate == "PT404"
    assert post_row(db, post["id"])["status"] == "pending_approval"


def test_an_approval_is_refused_when_its_post_is_no_longer_waiting(db, op):
    """어긋난 상태(승인은 pending인데 Post가 이미 draft)에서는 어느 쪽도 처리하지 않는다."""
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_service("n8n")
    db.conn.execute("update posts set status = 'draft' where id = %s", (post["id"],))
    db.as_operator(uid)
    for decision in ("approved", "rejected"):
        assert db.expect_error("select * from resolve_approval(%s, %s)", (approval["id"], decision)).sqlstate == "PT409"
    assert approval_of(db, post["id"])["status"] == "pending"
    assert post_row(db, post["id"])["status"] == "draft"


def test_approving_is_refused_when_the_asset_was_archived_after_submit(db, op):
    uid, pid = op
    post, approval, asset, _ = pending_post(db, uid, pid)
    db.as_operator(uid)
    db.val("select archive_asset(%s)", (asset["id"],))
    assert db.expect_error("select * from resolve_approval(%s, 'approved')", (approval["id"],)).sqlstate == "PT422"
    assert approval_of(db, post["id"])["status"] == "pending"
    assert post_row(db, post["id"])["status"] == "pending_approval"
    # 반려는 할 수 있다
    assert resolve(db, uid, approval["id"], "rejected")["status"] == "rejected"


# -----------------------------------------------------------------------------
# revise_post
# -----------------------------------------------------------------------------
def test_a_rejected_post_is_revised_and_resubmitted_with_a_new_approval(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "rejected", "다시 써 주세요")

    db.as_operator(uid)
    revised = db.one("select * from revise_post(%s, %s, %s)", (post["id"], "새 캡션", ["#new", "tag"]))
    assert revised["status"] == "draft" and revised["caption"] == "새 캡션" and revised["hashtags"] == ["new", "tag"]

    submit(db, uid, post["id"])
    db.as_postgres()
    statuses = [r["status"] for r in db.all("select status from approvals where post_id = %s order by created_at, id",
                                            (post["id"],))]
    assert sorted(statuses) == ["pending", "rejected"]                # 지난 승인은 그대로, 새 승인이 생겼다


def test_revise_without_changes_only_returns_the_post_to_draft(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "rejected")
    db.as_operator(uid)
    revised = db.one("select * from revise_post(%s)", (post["id"],))
    assert revised["status"] == "draft" and revised["caption"] == post["caption"]


def test_only_a_rejected_post_can_be_revised(db, op):
    uid, pid = op
    asset = ready_asset(db, uid, pid)
    post = create(db, uid, asset["id"])
    db.as_operator(uid)
    assert db.expect_error("select * from revise_post(%s)", (post["id"],)).sqlstate == "PT409"
    approved, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "approved")
    db.as_operator(uid)
    assert db.expect_error("select * from revise_post(%s)", (approved["id"],)).sqlstate == "PT409"


def test_revise_rejects_a_blank_caption(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "rejected")
    db.as_operator(uid)
    assert db.expect_error("select * from revise_post(%s, '   ')", (post["id"],)).sqlstate == "PT422"
    assert post_row(db, post["id"])["status"] == "rejected"


# -----------------------------------------------------------------------------
# cancel_post
# -----------------------------------------------------------------------------
def test_cancelling_a_pending_post_cancels_its_pending_approval(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_operator(uid)
    result = db.one("select * from cancel_post(%s, %s)", (post["id"], "마음이 바뀜"))
    assert result["status"] == "cancelled"
    after = approval_of(db, post["id"], "cancelled")
    assert after["id"] == approval["id"] and after["resolved_at"] is not None
    assert transitions(db, "post", post["id"])[-1]["reason"] == "operator:cancel_post: 마음이 바뀜"


@pytest.mark.parametrize("stage", ["draft", "approved", "rejected", "failed"])
def test_a_post_can_be_cancelled_from_these_states(db, op, stage):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    if stage == "draft":
        resolve(db, uid, approval["id"], "rejected")
        db.as_operator(uid)
        db.val("select revise_post(%s)", (post["id"],))
    elif stage == "approved":
        resolve(db, uid, approval["id"], "approved")
    elif stage == "rejected":
        resolve(db, uid, approval["id"], "rejected")
    else:
        resolve(db, uid, approval["id"], "approved")
        db.as_service("n8n")
        db.conn.execute("update posts set status = 'publishing' where id = %s", (post["id"],))
        db.conn.execute("update posts set status = 'failed', error = 'boom' where id = %s", (post["id"],))
    db.as_operator(uid)
    assert db.one("select * from cancel_post(%s)", (post["id"],))["status"] == "cancelled"


def test_cancelling_cancels_a_pending_publish_job(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "approved")
    db.as_service("n8n")
    job = db.one("select * from create_automation_job(p_job_type => 'publish', p_persona_id => %s, p_post_id => %s,"
                 " p_worker => 'n8n', p_idempotency_key => %s)", (pid, post["id"], f"publish:{post['id']}"))
    db.as_operator(uid)
    db.val("select cancel_post(%s)", (post["id"],))
    db.as_postgres()
    assert db.val("select status from automation_jobs where id = %s", (job["id"],)) == "cancelled"


def test_a_publishing_post_cannot_be_cancelled(db, op):
    """외부 SNS 호출이 이미 나갔을 수 있다. 결과가 나온 뒤에만 취소한다."""
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "approved")
    db.as_service("n8n")
    db.conn.execute("update posts set status = 'publishing' where id = %s", (post["id"],))
    db.as_operator(uid)
    assert db.expect_error("select * from cancel_post(%s)", (post["id"],)).sqlstate == "PT409"
    assert post_row(db, post["id"])["status"] == "publishing"


def test_published_and_cancelled_posts_cannot_be_cancelled(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "approved")
    db.as_service("n8n")
    db.conn.execute("update posts set status = 'publishing' where id = %s", (post["id"],))
    db.conn.execute("update posts set status = 'published', external_post_id = 'e1' where id = %s", (post["id"],))
    db.as_operator(uid)
    assert db.expect_error("select * from cancel_post(%s)", (post["id"],)).sqlstate == "PT409"

    other, other_approval, _, _ = pending_post(db, uid, pid)
    db.as_operator(uid)
    db.val("select cancel_post(%s)", (other["id"],))
    assert db.expect_error("select * from cancel_post(%s)", (other["id"],)).sqlstate == "PT409"


def test_another_operator_cannot_cancel_my_post(db, two_operators):
    uid_a, pid_a, uid_b, _ = two_operators
    post, _, _, _ = pending_post(db, uid_a, pid_a)
    db.as_operator(uid_b)
    assert db.expect_error("select * from cancel_post(%s)", (post["id"],)).sqlstate == "PT404"
    assert post_row(db, post["id"])["status"] == "pending_approval"


# -----------------------------------------------------------------------------
# 승인 없이는 approved가 될 수 없다 (11.8, PRD 2번)
# -----------------------------------------------------------------------------
def test_a_post_cannot_become_approved_without_an_approved_approval_even_for_service_role(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_service("n8n")
    err = db.expect_error("update posts set status = 'approved' where id = %s", (post["id"],))
    assert err.sqlstate == "PT409"
    assert post_row(db, post["id"])["status"] == "pending_approval"


def test_a_post_cannot_become_rejected_without_a_rejected_approval(db, op):
    uid, pid = op
    post, _, _, _ = pending_post(db, uid, pid)
    db.as_service("n8n")
    assert db.expect_error("update posts set status = 'rejected' where id = %s", (post["id"],)).sqlstate == "PT409"


def test_a_rejected_approval_does_not_unlock_approved(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "rejected")
    db.as_service("n8n")
    assert db.expect_error("update posts set status = 'approved' where id = %s", (post["id"],)).sqlstate == "PT409"


def test_an_expired_approval_sends_the_post_back_to_draft(db, op):
    """R7. 만료를 거는 pg_cron은 예약 게시와 함께 만든다. 전이 자체는 지금 동작한다."""
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_service("n8n")
    db.conn.execute("update approvals set status = 'expired', resolved_at = now() where id = %s", (approval["id"],))
    assert post_row(db, post["id"])["status"] == "draft"


def test_resolved_approvals_cannot_be_reopened(db, op):
    uid, pid = op
    _, approval, _, _ = pending_post(db, uid, pid)
    resolve(db, uid, approval["id"], "approved")
    db.as_service("n8n")
    assert db.expect_error("update approvals set status = 'pending', resolved_at = null where id = %s",
                           (approval["id"],)).sqlstate == "PT409"


# -----------------------------------------------------------------------------
# approvals 테이블: 격리·직접 쓰기 금지·무결성
# -----------------------------------------------------------------------------
def test_approvals_are_isolated_between_operators(db, two_operators):
    uid_a, pid_a, uid_b, pid_b = two_operators
    pending_post(db, uid_a, pid_a)
    db.as_operator(uid_b)
    assert db.val("select count(*) from approvals") == 0
    db.as_operator(uid_a)
    assert db.val("select count(*) from approvals") == 1


def test_an_operator_cannot_write_approvals_directly(db, op):
    uid, pid = op
    post, approval, _, _ = pending_post(db, uid, pid)
    db.as_operator(uid)
    assert db.expect_error("update approvals set status = 'approved' where id = %s",
                           (approval["id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("delete from approvals where id = %s", (approval["id"],)).sqlstate == PERMISSION_DENIED
    assert db.expect_error("insert into approvals (user_id, persona_id, post_id) values (%s, %s, %s)",
                           (uid, pid, post["id"])).sqlstate == PERMISSION_DENIED
    assert post_row(db, post["id"])["status"] == "pending_approval"


def test_anon_cannot_read_approvals(db, op):
    uid, pid = op
    pending_post(db, uid, pid)
    db.as_anon()
    assert db.expect_error("select count(*) from approvals").sqlstate == PERMISSION_DENIED


def test_only_one_approval_can_be_pending_per_post(db, op):
    uid, pid = op
    post, _, _, _ = pending_post(db, uid, pid)
    db.as_service("n8n")
    err = db.expect_error("insert into approvals (user_id, persona_id, post_id) values (%s, %s, %s)",
                          (uid, pid, post["id"]))
    assert err.sqlstate == "23505"


def test_an_approval_must_match_the_posts_persona_and_the_approver_must_own_it(db, two_operators):
    uid_a, pid_a, uid_b, pid_b = two_operators
    asset = ready_asset(db, uid_a, pid_a)
    post = create(db, uid_a, asset["id"])
    db.as_service("n8n")
    # Persona가 Post의 것과 다르다
    assert db.expect_error("insert into approvals (user_id, persona_id, post_id) values (%s, %s, %s)",
                           (uid_b, pid_b, post["id"])).sqlstate == "PT422"
    # 승인자가 Persona의 소유자가 아니다
    assert db.expect_error("insert into approvals (user_id, persona_id, post_id) values (%s, %s, %s)",
                           (uid_b, pid_a, post["id"])).sqlstate == "PT422"


def test_the_pending_state_and_the_resolved_time_go_together(db, op):
    uid, pid = op
    post, _, _, _ = pending_post(db, uid, pid)
    db.as_postgres()
    assert db.expect_error("update approvals set resolved_at = now() where post_id = %s", (post["id"],)).sqlstate == "23514"


def test_approvals_are_published_to_realtime_like_posts(db):
    db.as_postgres()
    tables = {r[0] for r in db.conn.execute("select tablename from pg_publication_tables where pubname = 'supabase_realtime'")}
    assert {"posts", "approvals"} <= tables
