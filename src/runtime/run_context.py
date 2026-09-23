"""Ambient binding between a running harness and the legacy call sites.

The problem this solves: agents call `tools.tool_gateway` directly. Threading
an executor argument through every agent would make the migration needlessly
invasive, so each run binds its executor in a ContextVar.

Instead the harness binds an executor to the current run, and the single hook
in `tools/tool_gateway.py` looks it up. Two rules make this safe:

- **Inheritance across threads is explicit.** `contextvars` does *not*
  propagate into `ThreadPoolExecutor` workers, so ResearchAgent submits each
  query through `copy_context().run`. This keeps concurrent API runs isolated
  instead of relying on a process-global executor.
- **Unbound is the normal case.** With nothing bound, `current_executor()`
  returns None and the gateway behaves exactly as before. That is what keeps
  `python main.py` and the 99 legacy tests on their original code path.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:  # pragma: no cover
    from src.tools.executor import ToolExecutor

_current: contextvars.ContextVar[Optional["ToolExecutor"]] = contextvars.ContextVar(
    "harness_executor", default=None
)

def current_executor() -> Optional["ToolExecutor"]:
    """The executor for the current run, or None when no harness is active.

    Worker pools must explicitly copy the caller's context when submitting.
    """
    return _current.get()


def is_active() -> bool:
    return current_executor() is not None


@contextmanager
def bind_run(executor: "ToolExecutor") -> Iterator["ToolExecutor"]:
    """Bind `executor` for the duration of the block.

    ContextVar tokens make independent runs in different API worker threads
    safe; nested bindings in one thread also restore the outer binding.
    """
    token = _current.set(executor)
    try:
        yield executor
    finally:
        _current.reset(token)


def force_unbind() -> None:
    """Clear the current context binding. Intended for test teardown only."""
    _current.set(None)
