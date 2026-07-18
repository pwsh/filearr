"""Shared Procrastinate retry policy for the Meilisearch-touching tasks.

Meilisearch applies back-pressure under heavy write load (HTTP 429, transient
5xx, connection resets while a batch is being merged). A live run saw **7
``index_sync`` failures out of ~52k jobs**, each silently dropping a document
batch from the projection until the next full rebuild. Because every one of
these tasks is a *pure projection of Postgres truth* (idempotent upsert / delete
by explicit id — invariant 1), retrying is always safe: re-running re-derives the
same documents. A failure that survives the retry budget is then genuinely
exceptional and worth surfacing.

Backoff is **exponential with additive jitter**. The live failures arrived in a
burst (one overloaded Meili instance failing several concurrent ``index`` jobs at
once); retrying them in lockstep would re-create the very back-pressure that
caused the failure (a thundering herd). Jitter spreads the retries out.

Procrastinate 3.9's stock ``RetryStrategy`` has no jitter knob, so this is a
small ``BaseRetryStrategy`` subclass (API verified against the installed
``procrastinate==3.9.0`` — ``get_retry_decision(*, exception, job)`` returning a
``RetryDecision(retry_in=...)`` or ``None``).
"""

from __future__ import annotations

import random
from collections.abc import Callable

from procrastinate.jobs import Job
from procrastinate.retry import BaseRetryStrategy, RetryDecision

# ``max_attempts`` is compared against ``job.attempts`` (0 on the first run), so a
# value of 4 yields the initial run plus up to 4 retries at attempts 0..3, giving
# exponential waits of ~2s, 4s, 8s, 16s (+jitter) before the job is left failed.
MEILI_RETRY_MAX_ATTEMPTS = 4
MEILI_RETRY_BASE_SECONDS = 2.0
MEILI_RETRY_JITTER_SECONDS = 1.0
# FIX-8: hard ceiling on the (pre-jitter) exponential wait so a mis-tuned
# max_attempts can never schedule an absurd backoff. At the current budget the
# largest wait is 16s (attempt 3), well under the cap; the cap only bites if the
# attempt budget is raised.
MEILI_RETRY_MAX_WAIT_SECONDS = 300.0


class ExponentialRetry(BaseRetryStrategy):
    """Exponential backoff (``base * 2**attempt``) with additive [0, jitter) noise.

    Pure and deterministic under an injected ``rng`` so the wait schedule is unit
    testable. ``retry_exceptions`` is intentionally *not* filtered: Meili
    back-pressure surfaces as several unrelated exception types (httpx transport
    errors, ``MeilisearchApiError`` 429/503, timeouts), and these tasks are safe
    to retry on *any* failure, so we retry unconditionally up to the budget.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        base_seconds: float = MEILI_RETRY_BASE_SECONDS,
        jitter_seconds: float = 0.0,
        max_wait_seconds: float | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_seconds = base_seconds
        self.jitter_seconds = jitter_seconds
        self.max_wait_seconds = max_wait_seconds
        self._rng = rng

    def wait_seconds(self, attempt: int) -> float:
        """Deterministic backoff (excluding jitter) for ``attempt`` (0-based),
        clamped to ``max_wait_seconds`` when set (FIX-8 backoff cap)."""
        wait = self.base_seconds * (2**attempt)
        if self.max_wait_seconds is not None:
            wait = min(wait, self.max_wait_seconds)
        return wait

    def get_retry_decision(
        self, *, exception: BaseException, job: Job
    ) -> RetryDecision | None:
        if self.max_attempts is not None and job.attempts >= self.max_attempts:
            return None
        wait = self.wait_seconds(job.attempts)
        if self.jitter_seconds:
            wait += self._rng() * self.jitter_seconds
        return RetryDecision(retry_in={"seconds": wait})


# The single shared policy applied to every Meili-touching task decorator.
MEILI_RETRY = ExponentialRetry(
    max_attempts=MEILI_RETRY_MAX_ATTEMPTS,
    base_seconds=MEILI_RETRY_BASE_SECONDS,
    jitter_seconds=MEILI_RETRY_JITTER_SECONDS,
    max_wait_seconds=MEILI_RETRY_MAX_WAIT_SECONDS,
)
