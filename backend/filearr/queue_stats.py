"""Read-only observability over the Procrastinate job queue (T8).

Extraction throughput is surfaced without a second bookkeeping store: we read the
``procrastinate_jobs`` table directly. Everything here is a single cheap
aggregate query grouped by (queue, status) -- no per-row scans, no writes. The
table is Postgres-native (same DB as the ORM), so we reuse the app session.

Status meanings (procrastinate 3.x enum): ``todo`` = queued/waiting,
``doing`` = in flight, ``succeeded`` / ``failed`` / ``cancelled`` / ``aborted``
= terminal. Queue *depth* is ``todo`` (backlog). If the procrastinate schema is
not present yet (fresh DB before init_db ran apply_schema), we return an empty
snapshot rather than raising -- the stats endpoint must stay cheap and total.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# (queue, status) pairs we roll up. Restricting to known statuses keeps the
# JSON shape stable across procrastinate versions that add/rename states.
_COUNTED_STATUSES = ("todo", "doing", "succeeded", "failed", "cancelled", "aborted")


async def queue_snapshot(session: AsyncSession) -> dict:
    """Return a per-queue job-state rollup plus a flat ``extract`` summary.

    Shape::

        {
          "queues": {"extract": {"todo": 4200, "doing": 4, "succeeded": 812, ...}, ...},
          "extract": {"depth": 4200, "running": 4, "done": 812, "failed": 3},
        }

    One aggregate query over ``procrastinate_jobs``; read-only. On a DB without
    the procrastinate schema the whole snapshot is empty (``{"queues": {}, ...}``).
    """
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return {"queues": {}, "extract": {"depth": 0, "running": 0, "done": 0, "failed": 0}}

    rows = (
        await session.execute(
            text(
                "SELECT queue_name, status::text AS status, count(*) AS n "
                "FROM procrastinate_jobs GROUP BY queue_name, status"
            )
        )
    ).all()

    queues: dict[str, dict[str, int]] = {}
    for queue_name, status, n in rows:
        if status not in _COUNTED_STATUSES:
            continue
        queues.setdefault(queue_name, {})[status] = int(n)

    ex = queues.get("extract", {})
    extract = {
        "depth": ex.get("todo", 0),  # backlog waiting to run
        "running": ex.get("doing", 0),
        "done": ex.get("succeeded", 0),
        "failed": ex.get("failed", 0),
    }
    return {"queues": queues, "extract": extract}
