"""브릿지 보안 도우미 (TECH_DESIGN 15.6, 15.8, 15.21)."""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections import defaultdict, deque


def token_matches(token: str | None, valid_tokens: tuple[str, ...]) -> bool:
    """상수 시간 비교. 교체 기간에는 토큰 2개를 함께 받는다 (BRIDGE_TOKENS=새,이전)."""
    if not token:
        return False
    ok = False
    for valid in valid_tokens:
        ok |= secrets.compare_digest(token.encode(), valid.encode())
    return ok


class AuthFailureTracker:
    """같은 IP에서 토큰 오류가 limit회를 넘으면 block_sec 동안 차단한다."""

    def __init__(self, limit: int, block_sec: float, window_sec: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.block_sec = block_sec
        self.window_sec = window_sec
        self.clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    def is_blocked(self, ip: str) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        if self.clock() >= until:
            del self._blocked_until[ip]
            return False
        return True

    MAX_TRACKED_IPS = 10_000

    def record_failure(self, ip: str) -> bool:
        """실패를 기록하고, 이번 실패로 차단되면 True. 1분에 limit회를 **넘으면** 차단한다."""
        now = self.clock()
        if len(self._failures) > self.MAX_TRACKED_IPS:  # IP를 바꿔 가며 기록을 키우는 것 방지
            self._failures.clear()
        q = self._failures[ip]
        q.append(now)
        while q and now - q[0] > self.window_sec:
            q.popleft()
        if len(q) > self.limit:
            self._blocked_until[ip] = now + self.block_sec
            q.clear()
            return True
        return False


class RateLimiter:
    """초당 rate회 (토큰 버킷)."""

    def __init__(self, rate_per_sec: float, clock=time.monotonic):
        self.rate = rate_per_sec
        self.capacity = max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.clock = clock
        self.updated = clock()

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


_SECRET_PATTERNS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"(sb_secret_|sb_publishable_)[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"(?i)(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)((?:x-bridge-token|x-callback-token|apikey|cf-access-client-secret)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+"),
     r"\1[REDACTED]"),
]


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class RedactingFilter(logging.Filter):
    """로그 메시지에서 비밀값을 가린다 (15.21)."""

    def __init__(self, extra_secrets: tuple[str, ...] = ()):
        super().__init__()
        self.extra_secrets = extra_secrets

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message, self.extra_secrets)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True
