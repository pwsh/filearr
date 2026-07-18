"""Meili-touching tasks carry the shared exponential-backoff + jitter retry.

A live run saw 7 ``index_sync`` failures out of ~52k under transient Meili
back-pressure, each silently dropping a document batch. These pure projections of
Postgres truth are safe to retry, so every Meili-touching task decorator gets the
shared ``MEILI_RETRY`` policy. Pure unit tests (no DB, no Meili)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from filearr.retrying import (
    MEILI_RETRY,
    MEILI_RETRY_MAX_ATTEMPTS,
    ExponentialRetry,
)

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestWarning")


def _job(attempts: int):
    # get_retry_decision only reads job.attempts.
    return SimpleNamespace(attempts=attempts)


def test_wait_is_exponential_in_base():
    rs = ExponentialRetry(max_attempts=4, base_seconds=2.0)
    assert [rs.wait_seconds(a) for a in range(4)] == [2.0, 4.0, 8.0, 16.0]


def test_stops_retrying_at_max_attempts():
    rs = ExponentialRetry(max_attempts=4, base_seconds=2.0)
    # retries while attempts < max_attempts, stops once reached
    assert rs.get_retry_decision(exception=Exception(), job=_job(3)) is not None
    assert rs.get_retry_decision(exception=Exception(), job=_job(4)) is None
    assert rs.get_retry_decision(exception=Exception(), job=_job(5)) is None


def test_backoff_schedule_with_deterministic_jitter():
    # rng pinned to 0 -> exactly the exponential schedule; the decision's retry_at
    # is ~base*2**attempt seconds in the future.
    rs = ExponentialRetry(max_attempts=5, base_seconds=2.0, jitter_seconds=1.0, rng=lambda: 0.0)
    for attempt, expected in [(0, 2.0), (1, 4.0), (2, 8.0)]:
        before = datetime.now(UTC)
        d = rs.get_retry_decision(exception=Exception(), job=_job(attempt))
        delta = (d.retry_at - before).total_seconds()
        assert expected <= delta <= expected + 0.5  # small execution slack


def test_jitter_adds_bounded_noise():
    # Max jitter (rng=1) pushes the wait up by ~jitter_seconds but no further.
    base = 2.0
    jitter = 1.0
    lo = ExponentialRetry(max_attempts=5, base_seconds=base, jitter_seconds=jitter, rng=lambda: 0.0)
    hi = ExponentialRetry(max_attempts=5, base_seconds=base, jitter_seconds=jitter, rng=lambda: 1.0)
    b0 = datetime.now(UTC)
    d_lo = lo.get_retry_decision(exception=Exception(), job=_job(0))
    d_hi = hi.get_retry_decision(exception=Exception(), job=_job(0))
    lo_delta = (d_lo.retry_at - b0).total_seconds()
    hi_delta = (d_hi.retry_at - b0).total_seconds()
    assert lo_delta < hi_delta
    assert hi_delta <= base + jitter + 0.5


def test_shared_policy_shape():
    assert MEILI_RETRY.max_attempts == MEILI_RETRY_MAX_ATTEMPTS == 4
    assert MEILI_RETRY.base_seconds == 2.0
    assert MEILI_RETRY.jitter_seconds > 0  # jitter enabled (thundering-herd guard)


def test_index_sync_tasks_carry_the_policy():
    # Import registers the tasks on the shared app.
    import filearr.tasks.index_sync  # noqa: F401
    from filearr.worker import proc_app

    # FIX-8 (scan-scheduling storm): the retry policy now applies ONLY to the
    # incremental/rebuild projection tasks, whose FAILURE genuinely drops a doc
    # batch until retried. The periodic MAINTENANCE tasks (purge/reconcile/etc.)
    # lost their retry -- the next tick re-runs them, and self-retry there was one
    # source of the runaway attempts the reaper then compounded.
    for name in (
        "filearr.tasks.index_sync.sync_items",
        "filearr.tasks.index_sync.rebuild_index",
    ):
        assert proc_app.tasks[name].retry_strategy is MEILI_RETRY, name


def test_periodic_maintenance_tasks_have_no_retry_fix8():
    from filearr.worker import proc_app

    for name in (
        "filearr.worker.purge_recycle_bin",
        "filearr.worker.purge_item_versions",
        "filearr.worker.purge_alert_events",
        "filearr.worker.purge_job_history",
        "filearr.worker.nightly_reconcile",
        "filearr.worker.reconcile_meili",
        "filearr.worker.reap_shadow_indexes",
        "filearr.worker.schedule_scans",
        "filearr.worker.reap_stalled_jobs",
    ):
        assert proc_app.tasks[name].retry_strategy is None, name


def test_shared_policy_has_backoff_cap_fix8():
    # FIX-8 belt: a hard ceiling on the exponential wait so a raised attempt
    # budget can never schedule an absurd backoff.
    assert MEILI_RETRY.max_wait_seconds is not None
    assert MEILI_RETRY.wait_seconds(100) == MEILI_RETRY.max_wait_seconds
