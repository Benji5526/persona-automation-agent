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
