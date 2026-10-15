"""
Outbox drainer for the ``notification_jobs`` table.

Thin app-side entry point: wires the OmniCare notification store and processor into
ragit's generic drainer. The drain loop itself — per-pass stale reclaim,
``FOR UPDATE SKIP LOCKED`` claiming, failure retry on the next tick, graceful
SIGINT/SIGTERM — lives in :mod:`ragit.jobs.drainer`.

Run it directly:

    python -m app.workers.notification_drain

Configuration (all optional, validated by ``app.core.config.Settings``):

    NOTIFICATION_DRAIN_INTERVAL_SECONDS  seconds to sleep when the queue is empty
    NOTIFICATION_DRAIN_BATCH_SIZE       jobs delivered per pass

Multiple drainers can run concurrently: ``SKIP LOCKED`` keeps two workers from
delivering the same job, and the unique key on the feed's ``job_id`` keeps a
redelivery after a reclaimed lock from notifying a claimant twice.

Delivery here never touches the request path: the claim endpoints enqueue a job row
in the claim transaction, and this process is what turns it into a notification.
"""

from __future__ import annotations

import sys

from app.core.config import settings
from app.domain.notifications.outbox import NotificationJobProcessor, NotificationJobStore
from ragit.jobs.drainer import main as _ragit_main


def main() -> int:
    """Console entry point: drain the notification outbox until signaled."""
    store = NotificationJobStore()
    processor = NotificationJobProcessor()
    return _ragit_main(
        store,
        processor,
        interval=settings.notification_drain_interval_seconds,
        batch_size=settings.notification_drain_batch_size,
        worker_id=settings.worker_id,
    )


if __name__ == "__main__":
    sys.exit(main())
