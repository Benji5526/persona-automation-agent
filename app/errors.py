"""작업 오류 분류 (TECH_DESIGN 6.9 error_type, 13.12 error_code)."""

from __future__ import annotations


class JobError(Exception):
    """Automation Job을 실패 처리해야 하는 오류.

    error_type: 6.9 분류 (transient, api, timeout, generation, validation, authentication, policy, unknown)
    error_code: 13.12 상세 코드 (예: OUT_OF_MEMORY)
    retryable: True면 fail_automation_job이 attempts < max_attempts일 때 재시도 대기로 돌린다.
    """

    def __init__(self, message: str, *, error_type: str, error_code: str, retryable: bool, step: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.error_code = error_code
        self.retryable = retryable
        self.step = step


def validation(code: str, message: str) -> JobError:
    return JobError(message, error_type="validation", error_code=code, retryable=False)


def transient(code: str, message: str) -> JobError:
    return JobError(message, error_type="transient", error_code=code, retryable=True)


class LockLost(Exception):
    """잠금을 잃음 (Heartbeat 회수, 취소). 결과를 버리고 조용히 멈춘다 (11.6)."""


class DbError(Exception):
    """Supabase(PostgREST) 오류. code는 SQLSTATE (예: PT409), message는 오류 코드 (12.2)."""

    def __init__(self, status: int, code: str | None, message: str, details: str | None = None):
        super().__init__(f"{status} {code}: {message} {details or ''}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class WorkerAuthError(Exception):
    """Worker 자격이 없거나 만료·폐기됐다 (TECH_DESIGN 56.5).

    같은 자격으로 다시 두드리지 않는다: 새 Job 선점을 멈추고 정상 종료한다 (사양 21조).
    """


# PostgREST의 JWT 거부 코드. 42501은 권한 없는 RPC·테이블 접근.
AUTH_FAILURE_CODES = frozenset({"PGRST301", "PGRST302", "PGRST303", "42501"})


def is_worker_auth_failure(exc: DbError) -> bool:
    """자격 자체가 깨진 경우만 True. 업무 규칙 위반(PT409 등)은 False다."""
    if exc.status == 401:
        return True
    if exc.code in AUTH_FAILURE_CODES:
        return True
    blob = f"{exc.message} {exc.details or ''}".lower()
    return exc.status == 403 and "worker credential" in blob
