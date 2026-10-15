"""Tests for the notification outbox and the in-app feed it fills.

The outbox mirrors the claim embedding outbox deliberately, so these tests pin the
same invariants for the notification table: the caller's session is flushed into
rather than committed, a claim transition writes exactly one job, the claim guard
keeps two drainers from delivering the same job, and a failing provider retries with
backoff and dead-letters rather than being retried forever.

Every scenario runs against the real tables. ``tests/conftest.py`` wipes the user
and claim tables before each test, and both notification tables reference ``users``
with a cascading reference, so a reset clears them too; this module still deletes its
own rows first, so a test can assert absolute counts without depending on that.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from ragit.jobs import process_pending_jobs as _ragit_process_pending_jobs
from sqlalchemy import func, select, text, update

from app.api.v1 import claims as claims_api
from app.core.database import async_session_factory, engine
from app.domain.notifications.outbox import (
    NotificationJobProcessor,
    NotificationJobStore,
    enqueue_notification_job,
    process_pending_notification_jobs,
    render_notification,
)
from app.models.claims.claim import Claim
from app.models.platform.notification_job import (
    CHANNEL_EMAIL,
    JOB_TYPE_CLAIM_STATUS_CHANGED,
    JOB_TYPE_CLAIM_SUBMITTED,
    JOB_TYPE_CLAIM_WITHDRAWN,
    MAX_ATTEMPTS,
    Notification,
    NotificationJob,
)
from app.models.user import User

_TEST_PASSWORD = "NotifyPass123!"  # noqa: S105 - fixture credential, not a secret
_PINNED_EPOCH = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_notification_tables() -> Iterator[None]:
    """Empty the two notification tables before each test.

    Children first: ``notifications`` references ``notification_jobs``, so a delete
    in the other order would trip the foreign key.

    Yields:
        None: The tables are empty when the test body starts.
    """

    async def _clear() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                # Table names are the two this module owns, never test input.
                await session.execute(text("DELETE FROM notifications"))
                await session.execute(text("DELETE FROM notification_jobs"))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_clear())
    yield


@pytest.fixture
def as_claims_handler(monkeypatch: pytest.MonkeyPatch):
    """Grant one caller the status endpoint's interim administrative authority.

    The endpoint cannot prove a caller is a claims handler until an authorization
    model exists, so this widens the seam the endpoint documents for the credential
    the request already carries.

    Args:
        monkeypatch: Pytest's monkeypatch fixture, which restores the seam when the
            test ends.

    Returns:
        Callable[[uuid.UUID], None]: A function granting authority to one user.
    """

    def _grant(user_id: uuid.UUID) -> None:
        granted = set(claims_api.ADMINISTRATIVE_TRANSITION_ACTORS)
        granted.add(str(user_id))
        monkeypatch.setattr(claims_api, "ADMINISTRATIVE_TRANSITION_ACTORS", frozenset(granted))

    return _grant


@pytest.fixture
def notifications_client(test_client: TestClient) -> Iterator[TestClient]:
    """Expose the notification routes for the duration of one test.

    The router is registered by ``app/api/v1/router.py``, which this track does not
    own, so the tests mount the same router object themselves and remove the routes
    again afterwards. Removing them matters: a later version of the router that
    registers them for real must still see exactly one of each.

    Yields:
        TestClient: The client, with the notification routes mounted.
    """
    from app.api.v1.notifications import router as notifications_router
    from app.main import app

    original_routes = list(app.router.routes)
    app.include_router(notifications_router, prefix="/api/v1/notifications", tags=["Notifications"])
    try:
        yield test_client
    finally:
        app.router.routes[:] = original_routes


def _register_user() -> uuid.UUID:
    """Create a user, because both notification tables reference ``users``.

    Returns:
        uuid.UUID: The new user's id.
    """

    async def _write() -> uuid.UUID:
        user_id = uuid.uuid4()
        async with async_session_factory() as session:
            session.add(
                User(
                    id=user_id,
                    username=f"notify_{user_id.hex[:12]}",
                    password_hash="not-a-real-hash",  # noqa: S106 - fixture row, never verified
                )
            )
            await session.commit()
        return user_id

    return asyncio.run(_write())


def _seed_claim(owner_id: uuid.UUID, claim_id: str, status: str = "Submitted") -> None:
    """Seed one claim so a lifecycle endpoint has something to move.

    Args:
        owner_id: The user the claim belongs to.
        claim_id: The human-readable claim identifier.
        status: The status the claim starts from.
    """

    async def _write() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                session.add(
                    Claim(
                        claim_id=claim_id,
                        policy_number=f"POL-{claim_id[-4:]}",
                        claim_type="Water Damage",
                        status=status,
                        amount=Decimal("1500.00"),
                        description=f"Factual description for {claim_id}.",
                        owner_id=owner_id,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_write())


def _signup(client: TestClient, username: str) -> tuple[uuid.UUID, dict[str, str]]:
    """Register a user through the public signup endpoint.

    Args:
        client: The test client to sign up through.
        username: A unique username for this user.

    Returns:
        tuple[uuid.UUID, dict[str, str]]: The user's id and their bearer headers.
    """
    response = client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": _TEST_PASSWORD},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


def _transition_payload(  # noqa: PLR0913
    *,
    claim_id: str,
    claim_uuid: uuid.UUID,
    user_id: uuid.UUID,
    from_status: str | None = None,
    to_status: str = "Under Review",
    note: str | None = None,
) -> dict[str, Any]:
    """Build the payload the claim lifecycle enqueues for a transition.

    Args:
        claim_id: The human-readable claim identifier.
        claim_uuid: The claim row's internal UUID.
        user_id: The user the notification belongs to.
        from_status: The status the claim held before the transition.
        to_status: The status the claim moved to.
        note: Optional reason recorded on the transition.

    Returns:
        dict[str, Any]: The payload the template renders from.
    """
    return {
        "claim_id": claim_id,
        "claim_uuid": str(claim_uuid),
        "user_id": str(user_id),
        "from_status": from_status,
        "to_status": to_status,
        "note": note,
    }


def _seed_job(
    user_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any],
    *,
    channel: str = "in_app",
    max_attempts: int = MAX_ATTEMPTS,
) -> uuid.UUID:
    """Seed one pending notification job and return its id.

    Args:
        user_id: The recipient.
        job_type: The delivery instruction.
        payload: The job's business payload.
        channel: The delivery channel the job names.
        max_attempts: Attempt ceiling before dead-lettering.

    Returns:
        uuid.UUID: The new job's id.
    """

    async def _write() -> uuid.UUID:
        job_id = uuid.uuid4()
        async with async_session_factory() as session:
            session.add(
                NotificationJob(
                    id=job_id,
                    user_id=user_id,
                    job_type=job_type,
                    payload=payload,
                    channel=channel,
                    status="pending",
                    status_detail="pending",
                    max_attempts=max_attempts,
                )
            )
            await session.commit()
        return job_id

    return asyncio.run(_write())


def _job_row(job_id: uuid.UUID) -> NotificationJob:
    """Read one outbox row back.

    Args:
        job_id: The job to read.

    Returns:
        NotificationJob: The row.
    """

    async def _read() -> NotificationJob:
        async with async_session_factory() as session:
            return await session.get(NotificationJob, job_id)

    return asyncio.run(_read())


def _all_jobs() -> list[NotificationJob]:
    """Read every outbox row back.

    Returns:
        list[NotificationJob]: All ``notification_jobs`` rows.
    """

    async def _read() -> list[NotificationJob]:
        async with async_session_factory() as session:
            return list((await session.execute(select(NotificationJob))).scalars().all())

    return asyncio.run(_read())


def _count_jobs() -> int:
    """Count every outbox row.

    Returns:
        int: The number of ``notification_jobs`` rows.
    """

    async def _count() -> int:
        async with async_session_factory() as session:
            return await session.scalar(select(func.count()).select_from(NotificationJob))

    return asyncio.run(_count())


def _claim_uuid(owner_id: uuid.UUID, claim_id: str) -> uuid.UUID:
    """Read a claim row's internal UUID.

    Args:
        owner_id: The user who owns the claim.
        claim_id: The human-readable claim identifier.

    Returns:
        uuid.UUID: The claim's id.
    """

    async def _read() -> uuid.UUID:
        async with async_session_factory() as session:
            return await session.scalar(
                select(Claim.id).where(Claim.claim_id == claim_id, Claim.owner_id == owner_id)
            )

    return asyncio.run(_read())


def _feed_rows(user_id: uuid.UUID | None = None) -> list[Notification]:
    """Read delivered notifications back, newest first.

    Args:
        user_id: Optional owner to filter on. When omitted every row is returned.

    Returns:
        list[Notification]: The delivered rows.
    """

    async def _read() -> list[Notification]:
        async with async_session_factory() as session:
            stmt = select(Notification).order_by(Notification.created_at.desc())
            if user_id is not None:
                stmt = stmt.where(Notification.user_id == user_id)
            return list((await session.execute(stmt)).scalars().all())

    return asyncio.run(_read())


def _deliver(limit: int = 10, worker_id: str | None = "test-worker") -> int:
    """Run one drain pass and return how many jobs were delivered.

    Args:
        limit: Batch size for the pass.
        worker_id: Identity stamped onto the jobs the pass claims.

    Returns:
        int: Number of jobs delivered.
    """
    return asyncio.run(process_pending_notification_jobs(limit=limit, worker_id=worker_id))


def _expire_retry_at(job_id: uuid.UUID) -> None:
    """Roll a failed job's retry timestamp into the past.

    Args:
        job_id: The job to make retry-due.
    """

    async def _write() -> None:
        async with async_session_factory() as session:
            job = await session.get(NotificationJob, job_id)
            if job is not None and job.next_retry_at is not None:
                job.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(_write())


def _pin_delivery_order(delivered_ids: list[uuid.UUID]) -> None:
    """Assign distinct delivery timestamps so an ordering assertion is deterministic.

    ``notifications.created_at`` takes the database clock, and rows written in one
    drain pass share that transaction's clock, so the tie-break on a random UUID
    would otherwise make "newest first" untestable.

    Args:
        delivered_ids: The notification ids, oldest first.
    """

    async def _write() -> None:
        async with async_session_factory() as session:
            for index, notification_id in enumerate(delivered_ids):
                await session.execute(
                    update(Notification)
                    .where(Notification.id == notification_id)
                    .values(created_at=_PINNED_EPOCH + timedelta(minutes=index))
                )
            await session.commit()

    asyncio.run(_write())


def _deliver_a_feed(user_id: uuid.UUID, *claim_ids: str) -> list[uuid.UUID]:
    """Queue and deliver one notification per claim id for one user.

    The delivered timestamps are pinned as they are queued, so the returned ids are
    in ascending delivery order and the API's newest-first page is their reverse.

    Args:
        user_id: The recipient of every notification.
        *claim_ids: One delivered notification per claim identifier, in order.

    Returns:
        list[uuid.UUID]: The delivered notification ids, oldest first.
    """
    for claim_id in claim_ids:
        _seed_job(
            user_id,
            JOB_TYPE_CLAIM_STATUS_CHANGED,
            _transition_payload(
                claim_id=claim_id,
                claim_uuid=uuid.uuid4(),
                user_id=user_id,
                from_status="Submitted",
                to_status="Under Review",
            ),
        )
    assert _deliver() == len(claim_ids)
    ordered = [row.id for row in sorted(_feed_rows(user_id), key=lambda row: row.claim_id)]
    _pin_delivery_order(ordered)
    return ordered


class _CountingProcessor:
    """Delivery counter around the real processor.

    The count is the double-delivery assertion: two workers that both reach
    ``process`` for one job would both notify, however the rest of the machinery
    behaved. The real processor still runs, so the feed row is asserted too.
    """

    def __init__(self) -> None:
        self._delegate = NotificationJobProcessor()
        self.delivered: list[str] = []

    async def process(self, job: Any) -> None:
        """Count and then perform the delivery.

        Args:
            job: The claimed job.
        """
        self.delivered.append(str(job.job_id))
        await self._delegate.process(job)


class _RecordingSession:
    """Session double that records flush and commit calls separately."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0

    def add(self, obj: object) -> None:
        """Record an object staged on the session."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Record a flush."""
        self.flushes += 1

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1


def test_enqueue_with_caller_session_does_not_commit() -> None:
    """Passing a session must only add and flush, never commit."""
    user_id = _register_user()
    session = _RecordingSession()

    asyncio.run(
        enqueue_notification_job(
            job_type=JOB_TYPE_CLAIM_SUBMITTED,
            user_id=user_id,
            payload={"claim_id": "CLM-FLUSH"},
            session=session,  # type: ignore[arg-type]
        )
    )

    assert session.commits == 0, "enqueue committed a caller-owned session"
    assert session.flushes == 1
    assert len(session.added) == 1


def test_rolled_back_claim_change_leaves_no_notification_job() -> None:
    """A transition transaction that rolls back must not leave a job behind.

    This is the invariant the enqueue hook exists for: a job committed on its own
    session would survive the rollback and tell a claimant their claim moved when it
    did not.
    """
    user_id = _register_user()

    async def _stage_then_rollback() -> None:
        async with async_session_factory() as session:
            await enqueue_notification_job(
                job_type=JOB_TYPE_CLAIM_STATUS_CHANGED,
                user_id=user_id,
                payload=_transition_payload(
                    claim_id="CLM-RBKT",
                    claim_uuid=uuid.uuid4(),
                    user_id=user_id,
                    to_status="Under Review",
                ),
                session=session,
            )
            await session.rollback()

    asyncio.run(_stage_then_rollback())

    assert _count_jobs() == 0


def test_enqueue_without_session_commits_its_own_transaction() -> None:
    """With no session the enqueue is self-contained and durable."""
    user_id = _register_user()

    asyncio.run(
        enqueue_notification_job(
            job_type=JOB_TYPE_CLAIM_SUBMITTED,
            user_id=user_id,
            payload={"claim_id": "CLM-OWNED"},
        )
    )

    assert _count_jobs() == 1


def test_one_status_transition_enqueues_exactly_one_notification_job(
    test_client: TestClient,
    as_claims_handler,
) -> None:
    """A transition writes one job, not one per hook it runs through."""
    owner_id, headers = _signup(test_client, "notify_one_transition")
    as_claims_handler(owner_id)
    _seed_claim(owner_id, "CLM-NFC1")
    assert _count_jobs() == 0

    response = test_client.post(
        "/api/v1/claims/CLM-NFC1/status",
        json={"status": "Under Review", "note": "A handler picked it up."},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    jobs = _all_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_type == JOB_TYPE_CLAIM_STATUS_CHANGED
    assert job.user_id == owner_id
    assert job.status == "pending"
    assert job.payload == {
        "claim_id": "CLM-NFC1",
        "claim_uuid": str(_claim_uuid(owner_id, "CLM-NFC1")),
        "user_id": str(owner_id),
        "from_status": "Submitted",
        "to_status": "Under Review",
        "note": "A handler picked it up.",
    }


def test_a_withdrawal_enqueues_the_withdrawn_notification(test_client: TestClient) -> None:
    """Withdrawing is its own delivery kind, not a status change."""
    owner_id, headers = _signup(test_client, "notify_withdraw")
    _seed_claim(owner_id, "CLM-NFC2")

    response = test_client.post(
        "/api/v1/claims/CLM-NFC2/withdraw",
        json={"reason": "Moved house"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    jobs = _all_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_type == JOB_TYPE_CLAIM_WITHDRAWN
    assert jobs[0].payload["to_status"] == "Withdrawn"
    assert jobs[0].payload["note"] == "Moved house"


def test_a_refused_transition_enqueues_nothing(test_client: TestClient) -> None:
    """A transition the lifecycle refuses writes no notification either."""
    owner_id, headers = _signup(test_client, "notify_refused")
    _seed_claim(owner_id, "CLM-NFC3", status="Approved")

    response = test_client.post(
        "/api/v1/claims/CLM-NFC3/status",
        json={"status": "Withdrawn"},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert _count_jobs() == 0


def test_a_confirmed_submission_enqueues_the_submitted_notification(
    test_client: TestClient,
) -> None:
    """Filing a claim tells the claimant it was received, durably."""
    _, headers = _signup(test_client, "notify_submitted")

    prepared = test_client.post(
        "/api/v1/claims/prepare",
        json={
            "policy_number": "POL-4417",
            "claim_type": "Water Damage",
            "amount": 1875.25,
            "description": "A frozen pipe burst flooded the basement overnight.",
        },
        headers=headers,
    )
    assert prepared.status_code == 200
    confirmed = test_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
        headers=headers,
    )

    assert confirmed.status_code == 200, confirmed.text
    jobs = _all_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_type == JOB_TYPE_CLAIM_SUBMITTED
    assert jobs[0].payload["claim_id"] == confirmed.json()["claim_id"]
    assert jobs[0].payload["claim_type"] == "Water Damage"
    assert jobs[0].payload["status"] == "Submitted"


def test_delivery_materialises_one_feed_row() -> None:
    """A claimed job becomes exactly one row in the owner's feed."""
    user_id = _register_user()
    _seed_job(
        user_id,
        JOB_TYPE_CLAIM_STATUS_CHANGED,
        _transition_payload(
            claim_id="CLM-DLVR",
            claim_uuid=uuid.uuid4(),
            user_id=user_id,
            from_status="Submitted",
            to_status="Under Review",
        ),
    )

    assert _deliver() == 1

    rows = _feed_rows(user_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.job_type == JOB_TYPE_CLAIM_STATUS_CHANGED
    assert row.claim_id == "CLM-DLVR"
    assert row.channel == "in_app"
    assert row.read_at is None
    assert row.title == "Claim CLM-DLVR is now Under Review"
    assert row.body == "Claim CLM-DLVR moved from Submitted to Under Review."


def test_redelivery_does_not_notify_twice() -> None:
    """A job delivered again after a reclaimed lock leaves one feed row."""
    user_id = _register_user()
    _seed_job(
        user_id,
        JOB_TYPE_CLAIM_WITHDRAWN,
        _transition_payload(
            claim_id="CLM-IDEM",
            claim_uuid=uuid.uuid4(),
            user_id=user_id,
            from_status="Submitted",
            to_status="Withdrawn",
            note="Moved house",
        ),
    )

    assert _deliver(worker_id="worker-a") == 1

    # A worker that died mid-pass leaves its row locked in ``processing``; the
    # replacement claims it again and delivers it a second time.
    async def _requeue_and_deliver() -> int:
        async with async_session_factory() as session:
            jobs = (await session.execute(select(NotificationJob))).scalars().all()
            for job in jobs:
                job.status = "pending"
            await session.commit()
        return await process_pending_notification_jobs(limit=10, worker_id="worker-b")

    assert asyncio.run(_requeue_and_deliver()) == 1

    rows = _feed_rows(user_id)
    assert len(rows) == 1
    assert rows[0].body == "You withdrew claim CLM-IDEM. Reason: Moved house"


def test_two_workers_claim_concurrently_without_double_delivery() -> None:
    """SKIP LOCKED plus the claim guard lets two drainers run without duplicates."""
    user_id = _register_user()
    _seed_job(
        user_id,
        JOB_TYPE_CLAIM_SUBMITTED,
        {"claim_id": "CLM-RACE", "claim_type": "Water Damage"},
    )

    async def _run() -> list[str]:
        first = _CountingProcessor()
        second = _CountingProcessor()
        store = NotificationJobStore()
        # Both passes run on the same loop and both select the same pending row:
        # exactly one of them may reach ``process``.
        await asyncio.gather(
            _ragit_process_pending_jobs(store, first, limit=10, worker_id="worker-a"),
            _ragit_process_pending_jobs(store, second, limit=10, worker_id="worker-b"),
        )
        return first.delivered + second.delivered

    delivered = asyncio.run(_run())

    assert len(delivered) == 1
    assert len(_all_jobs()) == 1
    assert _all_jobs()[0].status == "completed"
    assert len(_feed_rows()) == 1


def test_a_reserved_channel_fails_instead_of_succeeding_silently() -> None:
    """An unimplemented provider must dead-letter, not drop the notification."""
    user_id = _register_user()
    job_id = _seed_job(
        user_id,
        JOB_TYPE_CLAIM_SUBMITTED,
        {"claim_id": "CLM-EMAIL", "claim_type": "Water Damage"},
        channel=CHANNEL_EMAIL,
    )

    assert _deliver() == 0

    job = _job_row(job_id)
    assert job.status == "failed"
    assert job.retry_count == 1
    assert f"'{CHANNEL_EMAIL}' channel" in (job.status_detail or "")
    assert _feed_rows() == []


def test_a_failed_processor_retries_with_backoff_and_dead_letters() -> None:
    """A failing provider backs off, then dead-letters at the attempt ceiling."""
    user_id = _register_user()
    job_id = _seed_job(
        user_id,
        JOB_TYPE_CLAIM_SUBMITTED,
        {"claim_id": "CLM-RETRY", "claim_type": "Water Damage"},
        max_attempts=2,
    )
    failure = AsyncMock(side_effect=OSError("provider unreachable"))

    with patch("app.domain.notifications.outbox.deliver_notification", failure):
        assert _deliver() == 0

        first = _job_row(job_id)
        assert first.status == "failed"
        assert first.retry_count == 1
        assert first.next_retry_at is not None
        assert first.next_retry_at > datetime.now(UTC)
        assert first.locked_at is None
        assert first.locked_by is None
        assert "provider unreachable" in (first.status_detail or "")
        assert _feed_rows() == []

        # The backoff elapses between passes, making the job retry-due.
        _expire_retry_at(job_id)
        assert _deliver() == 0

    final = _job_row(job_id)
    assert final.status == "dead_letter"
    assert final.retry_count == 2
    assert final.next_retry_at is None
    assert "Exhausted 2 attempts" in (final.status_detail or "")
    assert _feed_rows() == []


def test_a_delivered_job_is_not_claimable_again() -> None:
    """A completed job leaves the queue; the feed is the only thing it left behind."""
    user_id = _register_user()
    _seed_job(
        user_id,
        JOB_TYPE_CLAIM_SUBMITTED,
        {"claim_id": "CLM-DONE", "claim_type": "Water Damage"},
    )

    assert _deliver() == 1
    assert _deliver() == 0
    assert len(_feed_rows(user_id)) == 1


def test_a_pending_job_is_invisible_to_the_feed() -> None:
    """A notification that has not been delivered does not show up half-written."""
    user_id = _register_user()
    _seed_job(
        user_id,
        JOB_TYPE_CLAIM_SUBMITTED,
        {"claim_id": "CLM-PEND", "claim_type": "Water Damage"},
    )

    assert _feed_rows(user_id) == []
    assert _count_jobs() == 1


@pytest.mark.parametrize(
    ("job_type", "payload"),
    [
        (
            JOB_TYPE_CLAIM_SUBMITTED,
            {"claim_id": "CLM-TPL1", "claim_type": "Water Damage"},
        ),
        (
            JOB_TYPE_CLAIM_STATUS_CHANGED,
            {
                "claim_id": "CLM-TPL2",
                "from_status": "Submitted",
                "to_status": "Under Review",
                "note": None,
            },
        ),
        (
            JOB_TYPE_CLAIM_WITHDRAWN,
            {
                "claim_id": "CLM-TPL3",
                "from_status": "Submitted",
                "to_status": "Withdrawn",
                "note": "Sold the car",
            },
        ),
    ],
)
def test_templates_are_deterministic(job_type: str, payload: dict[str, Any]) -> None:
    """The same job type and payload always render the same text."""
    first_title, first_body = render_notification(job_type, payload)
    second_title, second_body = render_notification(job_type, payload)

    assert (first_title, first_body) == (second_title, second_body)
    assert payload["claim_id"] in first_title
    assert payload["claim_id"] in first_body
    assert first_title
    assert first_body


def test_an_unknown_job_type_has_no_template() -> None:
    """A job type with no template fails the attempt instead of delivering nothing."""
    with pytest.raises(ValueError, match="No notification template"):
        render_notification("claim.exploded", {"claim_id": "CLM-TPL4"})


def test_the_notification_list_is_owner_scoped_and_marks_read(
    notifications_client: TestClient,
) -> None:
    """One user's feed never contains another's, and a foreign id is not found."""
    owner_id, owner_headers = _signup(notifications_client, "notify_feed_owner")
    intruder_id, intruder_headers = _signup(notifications_client, "notify_feed_intruder")
    owner_feed = _deliver_a_feed(owner_id, "CLM-FEEDA", "CLM-FEEDB")
    _deliver_a_feed(intruder_id, "CLM-FEEDC")
    assert intruder_id != owner_id

    listed = notifications_client.get("/api/v1/notifications", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["count"] == 2
    # Newest first, so the second claim queued is the first row returned.
    assert [item["claim_id"] for item in body["notifications"]] == ["CLM-FEEDB", "CLM-FEEDA"]
    assert all(item["read_at"] is None for item in body["notifications"])

    unread = notifications_client.get(
        "/api/v1/notifications", params={"unread_only": "1"}, headers=owner_headers
    )
    assert unread.status_code == 200
    assert [item["claim_id"] for item in unread.json()["notifications"]] == [
        "CLM-FEEDB",
        "CLM-FEEDA",
    ]

    # The owner's oldest notification is the one acknowledged here.
    read = notifications_client.post(
        f"/api/v1/notifications/{owner_feed[0]}/read", headers=owner_headers
    )
    assert read.status_code == 200, read.text
    assert read.json()["read_at"] is not None

    after_read = notifications_client.get(
        "/api/v1/notifications", params={"unread_only": "1"}, headers=owner_headers
    )
    assert [item["claim_id"] for item in after_read.json()["notifications"]] == ["CLM-FEEDB"]

    replayed = notifications_client.post(
        f"/api/v1/notifications/{owner_feed[0]}/read", headers=owner_headers
    )
    assert replayed.status_code == 200

    # The intruder sees only their own feed...
    intruder_list = notifications_client.get("/api/v1/notifications", headers=intruder_headers)
    assert intruder_list.status_code == 200
    assert [item["claim_id"] for item in intruder_list.json()["notifications"]] == ["CLM-FEEDC"]

    # ...and the owner's notification id is reported as not found rather than
    # forbidden, so the response cannot confirm that the id exists.
    foreign = notifications_client.post(
        f"/api/v1/notifications/{owner_feed[0]}/read", headers=intruder_headers
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"

    unknown = notifications_client.post(
        f"/api/v1/notifications/{uuid.uuid4()}/read", headers=owner_headers
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"

    assert notifications_client.get("/api/v1/notifications").status_code == 401


def test_the_notification_list_paginates(notifications_client: TestClient) -> None:
    """limit and offset walk the feed without repeating a row."""
    owner_id, headers = _signup(notifications_client, "notify_pages")
    _deliver_a_feed(owner_id, "CLM-PAGE1", "CLM-PAGE2", "CLM-PAGE3")

    first = notifications_client.get(
        "/api/v1/notifications", params={"limit": 2, "offset": 0}, headers=headers
    )
    second = notifications_client.get(
        "/api/v1/notifications", params={"limit": 2, "offset": 2}, headers=headers
    )

    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["count"] == 2
    assert [item["claim_id"] for item in first_body["notifications"]] == ["CLM-PAGE3", "CLM-PAGE2"]
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["count"] == 1
    assert [item["claim_id"] for item in second_body["notifications"]] == ["CLM-PAGE1"]


def test_the_unread_filter_only_returns_unacknowledged_rows(
    notifications_client: TestClient,
) -> None:
    """A read notification leaves the unread list but stays in the feed."""
    owner_id, headers = _signup(notifications_client, "notify_unread")
    feed = _deliver_a_feed(owner_id, "CLM-UNR1", "CLM-UNR2")

    marked = notifications_client.post(f"/api/v1/notifications/{feed[0]}/read", headers=headers)
    assert marked.status_code == 200

    full = notifications_client.get("/api/v1/notifications", headers=headers)
    unread = notifications_client.get(
        "/api/v1/notifications", params={"unread_only": "1"}, headers=headers
    )

    assert full.json()["count"] == 2
    assert unread.json()["count"] == 1
    assert [item["claim_id"] for item in unread.json()["notifications"]] == ["CLM-UNR2"]


def test_main_wires_app_store_and_processor_into_ragit_drainer() -> None:
    """The entry point builds the OmniCare store and processor and passes settings."""
    from app.core.config import settings
    from app.workers import notification_drain

    with patch.object(notification_drain, "_ragit_main", return_value=0) as mock_main:
        assert notification_drain.main() == 0

    args, kwargs = mock_main.call_args
    store, processor = args
    assert isinstance(store, NotificationJobStore)
    assert isinstance(processor, NotificationJobProcessor)
    assert kwargs["interval"] == settings.notification_drain_interval_seconds
    assert kwargs["batch_size"] == settings.notification_drain_batch_size
    assert kwargs["worker_id"] == settings.worker_id


def test_notification_drain_settings_are_bounded() -> None:
    """Settings enforce the documented bounds rather than clamping at runtime."""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(notification_drain_interval_seconds=0)

    with pytest.raises(ValidationError):
        Settings(notification_drain_batch_size=0)
