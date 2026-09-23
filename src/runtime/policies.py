"""Reliability policies: retry/backoff, circuit breaking, concurrency, dedup,
side-effect controls.

Every policy here is deterministic given its inputs *except* jitter, which
takes an injectable RNG so tests can pin it.

The circuit breaker is per (tool, provider) and has three states:

    CLOSED --failures>=threshold--> OPEN --cooldown elapsed--> HALF_OPEN
      ^                                                            |
      +---------------- probe succeeded --------------------------+
                       (probe failed -> back to OPEN)

The legacy MCP gateway had a one-way latch instead: one protocol failure and
the whole process fell back to direct calls forever, with no way back. That is
a degradation policy, not a breaker, and it is kept (it is the right call for
MCP specifically) but is now visible as an event rather than a silent flag.
"""
from __future__ import annotations

import random
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from src.runtime.errors import CIRCUIT_TRIPPING, ErrorClass, is_retryable


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
class RetryPolicy(BaseModel):
    """Exponential backoff with full jitter, bounded by class and attempt count."""

    max_attempts: int = Field(default=3, ge=1)
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0
    multiplier: float = 2.0
    #: Full jitter (AWS-style): sleep = random(0, computed). Set False for tests.
    jitter: bool = True
    #: Rate-limited calls get a longer floor - retrying a 429 in 500ms is rude
    #: and usually just earns another 429.
    rate_limit_min_delay_s: float = 2.0

    def should_retry(self, error_class: ErrorClass, attempt: int) -> bool:
        """`attempt` is 1-based: the attempt that just failed."""
        if attempt >= self.max_attempts:
            return False
        return is_retryable(error_class)

    def backoff(self, attempt: int, error_class: ErrorClass = ErrorClass.UNKNOWN,
                rng: Optional[random.Random] = None) -> float:
        """Delay before attempt N+1, in seconds."""
        raw = min(self.base_delay_s * (self.multiplier ** max(0, attempt - 1)), self.max_delay_s)
        if error_class == ErrorClass.RATE_LIMIT:
            raw = max(raw, self.rate_limit_min_delay_s)
        if not self.jitter:
            return round(raw, 3)
        r = rng or random
        return round(r.uniform(0.0, raw), 3)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-key breaker. Thread-safe: tools run in ResearchAgent's thread pool."""

    def __init__(self, key: str, failure_threshold: int = 5, cooldown_s: float = 30.0,
                 half_open_successes: int = 1, clock: Callable[[], float] = time.monotonic) -> None:
        self.key = key
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.half_open_successes = half_open_successes
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._half_open_ok = 0
        self._opened_at = 0.0
        self.open_count = 0  # lifetime, for metrics

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._peek_state()

    def _peek_state(self) -> CircuitState:
        """Caller holds the lock. Promotes OPEN -> HALF_OPEN once cooled down."""
        if self._state == CircuitState.OPEN and self._clock() - self._opened_at >= self.cooldown_s:
            self._state = CircuitState.HALF_OPEN
            self._half_open_ok = 0
        return self._state

    def allow(self) -> tuple[bool, str]:
        """Whether a call may proceed right now."""
        with self._lock:
            state = self._peek_state()
            if state == CircuitState.OPEN:
                remaining = max(0.0, self.cooldown_s - (self._clock() - self._opened_at))
                return False, f"circuit open for {self.key}, retry in {remaining:.1f}s"
            return True, ""

    def record_success(self) -> None:
        with self._lock:
            if self._peek_state() == CircuitState.HALF_OPEN:
                self._half_open_ok += 1
                if self._half_open_ok >= self.half_open_successes:
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
            else:
                self._consecutive_failures = 0
                self._state = CircuitState.CLOSED

    def record_failure(self, error_class: ErrorClass) -> bool:
        """Returns True if this failure opened (or re-opened) the circuit.

        Only provider-health errors count. A 404 or a bad argument says nothing
        about whether the tool is up, and must not trip the breaker.
        """
        if error_class not in CIRCUIT_TRIPPING:
            return False
        with self._lock:
            state = self._peek_state()
            self._consecutive_failures += 1
            if state == CircuitState.HALF_OPEN or self._consecutive_failures >= self.failure_threshold:
                already_open = state == CircuitState.OPEN
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                if not already_open:
                    self.open_count += 1
                    return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_ok = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "key": self.key, "state": self._peek_state().value,
                "consecutive_failures": self._consecutive_failures,
                "open_count": self.open_count,
            }


class CircuitRegistry:
    """Lazily creates one breaker per key (tool name, or provider name)."""

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock

    def get(self, key: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = CircuitBreaker(key, self._failure_threshold, self._cooldown_s, clock=self._clock)
                self._breakers[key] = breaker
            return breaker

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [b.snapshot() for b in self._breakers.values()]

    def open_keys(self) -> list[str]:
        return [b.key for b in self._breakers.values() if b.state == CircuitState.OPEN]


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
class ConcurrencyLimiter:
    """Bounded concurrency with a global cap plus optional per-key caps.

    Used as a context manager so a raising tool still releases its slot.
    """

    def __init__(self, max_global: int = 8, per_key: Optional[dict[str, int]] = None) -> None:
        self._global = threading.BoundedSemaphore(max_global)
        self._per_key = {k: threading.BoundedSemaphore(v) for k, v in (per_key or {}).items()}
        self.max_global = max_global

    class _Slot:
        def __init__(self, acquired: list[threading.BoundedSemaphore]) -> None:
            self._acquired = acquired

        def __enter__(self) -> "ConcurrencyLimiter._Slot":
            return self

        def __exit__(self, *exc: Any) -> None:
            for sem in reversed(self._acquired):
                sem.release()

    def acquire(self, key: str = "", timeout: Optional[float] = None) -> "_Slot":
        acquired: list[threading.BoundedSemaphore] = []
        sem = self._per_key.get(key)
        try:
            if sem is not None:
                if not sem.acquire(timeout=timeout):
                    raise TimeoutError(f"concurrency slot for {key!r} not available")
                acquired.append(sem)
            if not self._global.acquire(timeout=timeout):
                raise TimeoutError("global concurrency slot not available")
            acquired.append(self._global)
        except BaseException:
            for s in reversed(acquired):
                s.release()
            raise
        return ConcurrencyLimiter._Slot(acquired)


# --------------------------------------------------------------------------- #
# Duplicate-call detection
# --------------------------------------------------------------------------- #
class DuplicateDetector:
    """Remembers (tool, args) fingerprints seen within one run.

    Distinguishes two things the legacy pipeline conflated:
    - *redundant*: same call issued again while the first result is still valid
      -> serve the memoized result, count it as waste;
    - *repeat after failure*: the previous attempt failed, so re-issuing is a
      legitimate recovery, not waste.
    """

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def seen(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._results

    def get(self, fingerprint: str) -> Any:
        with self._lock:
            self._counts[fingerprint] = self._counts.get(fingerprint, 0) + 1
            return self._results.get(fingerprint)

    def remember(self, fingerprint: str, result: Any) -> None:
        with self._lock:
            self._results.setdefault(fingerprint, result)
            self._counts.setdefault(fingerprint, 1)

    def duplicate_count(self) -> int:
        """Total calls beyond the first for each fingerprint."""
        with self._lock:
            return sum(max(0, c - 1) for c in self._counts.values())

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {k: v for k, v in self._counts.items() if v > 1}


# --------------------------------------------------------------------------- #
# Side-effect controls
# --------------------------------------------------------------------------- #
class SideEffectPolicy(BaseModel):
    """Controls for tools that could change external state.

    This project is read-only financial research: every registered tool today
    declares `side_effect_free=True` except the local file exporters. These
    fields exist so a future write tool inherits the controls by default rather
    than being bolted on later - they are deliberately NOT exercised by faking
    a write tool just to demo the feature.
    """

    approval_required: bool = False
    dry_run: bool = False
    idempotency_key: str = ""
    #: Name of the tool that undoes this one, if any.
    compensation_action: str = ""
    #: Approvals granted for this run, keyed by tool name.
    granted: dict[str, bool] = Field(default_factory=dict)

    def check(self, tool_name: str, side_effect_free: bool) -> tuple[bool, str]:
        """Returns (allowed, reason-if-blocked)."""
        if side_effect_free:
            return True, ""
        if self.dry_run:
            return False, f"dry_run active: {tool_name} would mutate external state"
        if self.approval_required and not self.granted.get(tool_name, False):
            return False, f"approval required for side-effecting tool {tool_name}"
        return True, ""


class CancellationToken:
    """Cooperative cancellation. Checked between attempts and between phases."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled by caller") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            from src.runtime.errors import RunCancelledError

            raise RunCancelledError(self.reason or "run cancelled")
