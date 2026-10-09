"""
Outbox drainer for the ``embedding_jobs`` table.

Thin app-side entry point: wires the
OmniCare job store and claim processor into ragit's
generic drainer. The drain loop itself — per-pass stale
reclaim, ``FOR UPDATE SKIP LOCKED`` claiming, failure
retry on the next tick, graceful SIGINT/SIGTERM — lives
in :mod:`ragit.jobs.drainer`.

Run it directly:

    python -m app.workers.embedding_drain

Configuration (all optional, validated by ``app.config.Settings``):

    EMBEDDING_DRAIN_INTERVAL_SECONDS  seconds to sleep when the queue is empty
    EMBEDDING_DRAIN_BATCH_SIZE       jobs claimed per pass

Multiple drainers can run concurrently: ``SKIP LOCKED`` keeps two workers from
processing the same job, so this scales horizontally without coordination.
"""

from __future__ import annotations

import sys

from app.config import settings
from app.domain.claims.outbox import ClaimJobProcessor, SqlAlchemyJobStore
from ragit.jobs.drainer import main as _ragit_main


def main() -> int:
    """Console entry point: drain the embedding outbox until signaled."""
    store = SqlAlchemyJobStore()
    processor = ClaimJobProcessor()
    return _ragit_main(
        store,
        processor,
        interval=settings.embedding_drain_interval_seconds,
        batch_size=settings.embedding_drain_batch_size,
        worker_id=settings.worker_id,
    )


if __name__ == "__main__":
    sys.exit(main())
