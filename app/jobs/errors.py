"""Job handler error types (architecture/12 §Retry).

A handler signals retry intent explicitly:
- `RetryableError(after=...)` — retry with the given backoff (overrides the
  queue's own exponential schedule when a provider's Retry-After is available).
- `TerminalError(code)` — move immediately to `dead`, no further retries.
- Any other unhandled exception — treated as retryable up to `max_attempts`.
"""

from __future__ import annotations


class RetryableError(Exception):
    """The handler knows it failed transiently and supplies an explicit
    retry delay.  `after` is seconds; `None` means use the queue's own
    exponential schedule.
    """

    def __init__(self, *, after: float | None = None) -> None:
        self.after = after
        super().__init__(f"retryable after {after}s" if after else "retryable")


class TerminalError(Exception):
    """The handler determined the failure is permanent — no retry, job goes
    straight to `dead`.  `code` is stored in `jobs.last_error`.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
