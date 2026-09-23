"""Structured error taxonomy for every tool and model call in the harness.

The single rule this module exists to enforce: **only transient errors may be
retried automatically.** An AUTH_ERROR retried three times is three wasted
calls and three more chances to trip a provider rate limit; an INVALID_ARGUMENT
retried is the same bad request sent again. The legacy pipeline retried on
`Exception` regardless of cause (agents/base_agent.py) and relied on an ad-hoc
`exc.non_retryable` attribute set by exactly one call site.

Classification is deliberately conservative: anything unrecognised becomes
`UNKNOWN`, which is *not* retryable. Silently retrying an unclassified error is
how a bug in one provider adapter turns into a bill.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional


class ErrorClass(str, Enum):
    """Canonical error classes. Persisted in traces, so values are stable."""

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTH_ERROR = "AUTH_ERROR"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    NOT_FOUND = "NOT_FOUND"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    UNKNOWN = "UNKNOWN"


#: Error classes that may be retried by the executor. Everything else is
#: terminal for that attempt. NOT_FOUND is excluded on purpose: a 404 on a
#: search result URL is a fact about the web, not a transient condition.
RETRYABLE: frozenset[ErrorClass] = frozenset(
    {ErrorClass.TIMEOUT, ErrorClass.RATE_LIMIT, ErrorClass.TRANSIENT_NETWORK}
)

#: Classes that indicate the *provider* (not just this call) is unhealthy and
#: should count toward opening a circuit breaker.
CIRCUIT_TRIPPING: frozenset[ErrorClass] = frozenset(
    {ErrorClass.TIMEOUT, ErrorClass.RATE_LIMIT, ErrorClass.TRANSIENT_NETWORK, ErrorClass.AUTH_ERROR}
)


def is_retryable(error_class: ErrorClass) -> bool:
    """Whether the executor may retry an attempt that failed with `error_class`."""
    return error_class in RETRYABLE


class HarnessError(Exception):
    """Base class for errors raised by the harness itself (not by tools).

    Carries the classification so callers never have to re-derive it, plus a
    `details` dict that goes into the trace event verbatim (after redaction).
    """

    error_class: ErrorClass = ErrorClass.UNKNOWN

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class.value,
            "message": self.message,
            "retryable": is_retryable(self.error_class),
            "details": self.details,
        }


class ToolNotFoundError(HarnessError):
    error_class = ErrorClass.NOT_FOUND


class InvalidArgumentError(HarnessError):
    error_class = ErrorClass.INVALID_ARGUMENT


class OutputSchemaError(HarnessError):
    error_class = ErrorClass.SCHEMA_ERROR


class PolicyBlockedError(HarnessError):
    """Raised when a policy (approval required, dry-run, deny-list) blocks a call."""

    error_class = ErrorClass.POLICY_BLOCKED


class BudgetExceededError(HarnessError):
    error_class = ErrorClass.BUDGET_EXCEEDED


class CircuitOpenError(HarnessError):
    """The breaker for this tool/provider is open; the call was not attempted."""

    error_class = ErrorClass.TRANSIENT_NETWORK


class RunCancelledError(HarnessError):
    error_class = ErrorClass.POLICY_BLOCKED


# --------------------------------------------------------------------------- #
# Classification of *foreign* exceptions (requests, litellm, mcp, akshare, ...)
# --------------------------------------------------------------------------- #

# Matched against the exception type name. Checked before the message patterns
# because a type name is a much stronger signal than substring matching.
_TYPE_NAME_RULES: tuple[tuple[re.Pattern[str], ErrorClass], ...] = (
    (re.compile(r"Timeout|Timedout", re.I), ErrorClass.TIMEOUT),
    (re.compile(r"RateLimit|TooManyRequests", re.I), ErrorClass.RATE_LIMIT),
    (re.compile(r"Authentication|Unauthorized|Forbidden|PermissionDenied", re.I), ErrorClass.AUTH_ERROR),
    (re.compile(r"ValidationError|SchemaError|JSONDecodeError", re.I), ErrorClass.SCHEMA_ERROR),
    (re.compile(r"NotFound|FileNotFound", re.I), ErrorClass.NOT_FOUND),
    (re.compile(r"ProxyError|SSLError|ConnectionError|ConnectTimeout|ReadTimeout|RemoteDisconnected|ChunkedEncoding", re.I),
     ErrorClass.TRANSIENT_NETWORK),
    (re.compile(r"^(TypeError|ValueError|KeyError|AttributeError|IndexError)$"), ErrorClass.INVALID_ARGUMENT),
)

# Matched against str(exception). Ordered: first match wins, so the more
# specific patterns come first.
_MESSAGE_RULES: tuple[tuple[re.Pattern[str], ErrorClass], ...] = (
    # Found by the live canary, not by any fixture: `tools/web_reader.py`
    # returns `empty_content_after_cleaning` when a page fetches fine but has
    # no text left after HTML cleaning, and the PDF reader returns "no text
    # layer" for scans. Both are permanent for that URL - retrying cannot
    # create text - yet both fell through to UNKNOWN. UNKNOWN is non-retryable,
    # so the behaviour happened to be right for the wrong reason, while every
    # metric mislabelled the cause.
    (re.compile(r"empty[_\s]?content|no text (layer|content)|content is empty|"
                r"empty (body|document|page)", re.I), ErrorClass.PERMANENT_FAILURE),
    (re.compile(r"\b429\b|rate.?limit|quota|too many requests|insufficient_quota", re.I), ErrorClass.RATE_LIMIT),
    (re.compile(r"\b401\b|\b403\b|api.?key|unauthorized|forbidden|invalid.?token", re.I), ErrorClass.AUTH_ERROR),
    (re.compile(r"timed? ?out|timeout|deadline exceeded", re.I), ErrorClass.TIMEOUT),
    (re.compile(r"\b404\b|not found|no such file", re.I), ErrorClass.NOT_FOUND),
    (re.compile(r"\b5\d\d\b|connection|network|dns|ssl|proxy|unreachable|reset by peer|temporarily", re.I),
     ErrorClass.TRANSIENT_NETWORK),
    (re.compile(r"schema|validation|invalid json|expecting value|json decode", re.I), ErrorClass.SCHEMA_ERROR),
    (re.compile(r"invalid.?argument|missing.?(required|parameter)|unexpected keyword", re.I), ErrorClass.INVALID_ARGUMENT),
    # Last resort for any *other* 4xx, including the non-standard codes that
    # anti-bot front-ends invent (the live canary produced a real
    # `468 Client Error` from an WAF). A 4xx means the request as sent is
    # unacceptable, so replaying it unchanged cannot succeed -> permanent.
    # This must stay at the end: the retryable 4xx (429 rate limit, 408
    # timeout) and the more specific 401/403/404 are all matched above, so
    # this rule can only catch what nothing else claimed.
    (re.compile(r"\b4\d\d\b[\s:]*client error", re.I), ErrorClass.PERMANENT_FAILURE),
)


def classify_exception(exc: BaseException) -> ErrorClass:
    """Map an arbitrary exception onto the taxonomy.

    Harness errors carry their own class. Everything else is matched first on
    exception type name, then on message text. Unmatched -> UNKNOWN (not
    retryable) rather than a hopeful guess at TRANSIENT_NETWORK.
    """
    if isinstance(exc, HarnessError):
        return exc.error_class

    type_name = type(exc).__name__
    for pattern, error_class in _TYPE_NAME_RULES:
        if pattern.search(type_name):
            return error_class

    message = str(exc) or ""
    for pattern, error_class in _MESSAGE_RULES:
        if pattern.search(message):
            return error_class

    return ErrorClass.UNKNOWN


def classify_http_status(status: int) -> ErrorClass:
    """Map an HTTP status code onto the taxonomy (for tools that return codes
    instead of raising)."""
    if status == 401 or status == 403:
        return ErrorClass.AUTH_ERROR
    if status == 404 or status == 410:
        return ErrorClass.NOT_FOUND
    if status == 408:
        return ErrorClass.TIMEOUT
    if status == 429:
        return ErrorClass.RATE_LIMIT
    if 500 <= status < 600:
        return ErrorClass.TRANSIENT_NETWORK
    if 400 <= status < 500:
        return ErrorClass.INVALID_ARGUMENT
    return ErrorClass.UNKNOWN


def describe(error_class: ErrorClass) -> str:
    """Human-readable one-liner used in reports and failure taxonomy output."""
    return {
        ErrorClass.TIMEOUT: "调用超时，未在时限内返回",
        ErrorClass.RATE_LIMIT: "被限流或配额耗尽",
        ErrorClass.AUTH_ERROR: "认证/授权失败（密钥无效或权限不足）",
        ErrorClass.INVALID_ARGUMENT: "调用参数不合法",
        ErrorClass.SCHEMA_ERROR: "返回结构不符合声明的 schema",
        ErrorClass.NOT_FOUND: "目标资源不存在",
        ErrorClass.TRANSIENT_NETWORK: "临时网络故障",
        ErrorClass.PERMANENT_FAILURE: "确定性失败，重试不会改变结果",
        ErrorClass.POLICY_BLOCKED: "被策略阻止（需审批/dry-run/黑名单）",
        ErrorClass.BUDGET_EXCEEDED: "预算耗尽",
        ErrorClass.UNKNOWN: "未分类错误（按不可重试处理）",
    }[error_class]
