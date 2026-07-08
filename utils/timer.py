"""Small timing helper used to measure step durations for the trace log."""
import time
from contextlib import contextmanager


class Timer:
    """Context manager that records elapsed wall-clock time in `self.elapsed`."""

    def __init__(self) -> None:
        self.start: float = 0.0
        self.elapsed: float = 0.0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.elapsed = round(time.perf_counter() - self.start, 4)


@contextmanager
def timed():
    """Functional-style alternative to the Timer class; yields a dict updated on exit."""
    state = {"elapsed": 0.0}
    start = time.perf_counter()
    try:
        yield state
    finally:
        state["elapsed"] = round(time.perf_counter() - start, 4)
