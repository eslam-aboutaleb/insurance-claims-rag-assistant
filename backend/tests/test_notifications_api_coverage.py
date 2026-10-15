"""Coverage tests for the remaining branches of ``app.api.v1.notifications``.

The owner scoping and the happy paths live in ``tests/test_notification_outbox.py``;
what this file pins are the branches a failure reaches and the edges of the
window the endpoint exposes: a caller whose id is not a UUID, a notification that
belongs to somebody else, and the pagination bounds.

Every scenario drives the public API with a real bearer token against the
isolated test database, where the feed is seeded directly so a test can fix who
owns which notification and when it was delivered.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.security.auth import get_current_user
from app.core.database import async_session_factory, engine
from app.models.platform.notification_job import JOB_TYPE_CLAIM_STATUS_CHANGED, Notification, NotificationJob

_TEST_PASSWORD = "NotifyCoverage123!"  # noqa: S105 - fixture credential, not a secret
_PINNED_EPOCH = datetime(2026, 5, 1, tzinfo=UTC)


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


def _seed_feed(
    owner_id: uuid.UUID,
    claim_ids: list[str],
    *,
    read: set[str] | None = None,
) -> list[uuid.UUID]:
    """Deliver one notification per claim id into one user's feed.

    Each notification is backed by its own completed outbox row, the way the
    drain worker writes them, and the delivery timestamps are pinned one second
    apart so the page order is deterministic rather than insertion-ordered.

    Args:
        owner_id: The recipient of every notification.
        claim_ids: The claims the notifications describe, oldest first.
        read: The claims whose notifications the caller has acknowledged.

    Returns:
        list[uuid.UUID]: The delivered notification ids, oldest first.
    """
    acknowledged = read or set()

    async def _write() -> list[uuid.UUID]:
        await engine.dispose()
        try:
            delivered: list[uuid.UUID] = []
            async with async_session_factory() as session:
                for index, claim_id in enumerate(claim_ids):
                    job = NotificationJob(
                        id=uuid.uuid4(),
                        user_id=owner_id,
                        job_type=JOB_TYPE_CLAIM_STATUS_CHANGED,
                        payload={"claim_id": claim_id},
                        status="completed",
                    )
                    session.add(job)
                    # The feed row references the outbox row, and the models
                    # declare no relationship, so the job is flushed first.
                    await session.flush()
                    notification = Notification(
                        id=uuid.uuid4(),
                        user_id=owner_id,
                        job_id=job.id,
                        job_type=JOB_TYPE_CLAIM_STATUS_CHANGED,
                        channel="in_app",
                        title=f"Claim {claim_id} is now Under Review",
                        body=f"Claim {claim_id} moved from Submitted to Under Review.",
                        claim_id=claim_id,
                        created_at=_PINNED_EPOCH + timedelta(seconds=index),
                        read_at=(
                            _PINNED_EPOCH + timedelta(seconds=index + 1)
                            if claim_id in acknowledged
                            else None
                        ),
                    )
                    session.add(notification)
                    delivered.append(notification.id)
                await session.commit()
            return delivered
        finally:
            await engine.dispose()

    return asyncio.run(_write())


def _with_invalid_user_id(test_client: TestClient) -> None:
    """Override authentication so the caller's id is not a valid UUID.

    Args:
        test_client: The client the override applies to.
    """

    async def _override() -> str:
        return "not-a-valid-uuid"

    from app.main import app  # noqa: PLC0415

    app.dependency_overrides[get_current_user] = _override


class TestInvalidUserId:
    """A caller the endpoint cannot parse is a 401, not a crash."""

    def test_listing_with_a_non_uuid_user_id_is_rejected(self, test_client: TestClient):
        """The feed is scoped on a UUID, so a caller it cannot parse has no feed."""
        _with_invalid_user_id(test_client)
        try:
            response = test_client.get("/api/v1/notifications")
        finally:
            test_client.app.dependency_overrides.pop(get_current_user, None)

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid user"

    def test_marking_read_with_a_non_uuid_user_id_is_rejected(self, test_client: TestClient):
        """The acknowledgement is scoped the same way, before any lookup."""
        _with_invalid_user_id(test_client)
        try:
            response = test_client.post(f"/api/v1/notifications/{uuid.uuid4()}/read")
        finally:
            test_client.app.dependency_overrides.pop(get_current_user, None)

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid user"


class TestForeignNotification:
    """Another user's notification is not found, not forbidden."""

    def test_marking_another_users_notification_is_reported_as_not_found(
        self, test_client: TestClient
    ):
        """The response must not confirm that the id exists.

        Both predicates live in the WHERE clause, so a notification belonging to
        somebody else is answered exactly like one that was never issued.
        """
        owner_id, owner_headers = _signup(test_client, "notify_cov_owner")
        _, intruder_headers = _signup(test_client, "notify_cov_intruder")
        owner_feed = _seed_feed(owner_id, ["CLM-COV1"])

        response = test_client.post(
            f"/api/v1/notifications/{owner_feed[0]}/read", headers=intruder_headers
        )

        assert response.status_code == 404
        assert response.json()["error"]["message"] == "Notification not found"
        # The intruder's refusal changed nothing: the row is still unread for its
        # owner, which is what a real scope check protects.
        still_unread = test_client.get(
            "/api/v1/notifications", params={"unread_only": "1"}, headers=owner_headers
        )
        assert [item["claim_id"] for item in still_unread.json()["notifications"]] == ["CLM-COV1"]

    def test_marking_a_notification_that_was_never_issued_is_not_found(
        self, test_client: TestClient
    ):
        """An unknown id is indistinguishable from a foreign one."""
        _, headers = _signup(test_client, "notify_cov_unknown")

        response = test_client.post(f"/api/v1/notifications/{uuid.uuid4()}/read", headers=headers)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestPaginationBounds:
    """The page window is validated rather than silently clamped."""

    def test_a_page_past_the_end_of_the_feed_is_empty_but_well_formed(
        self, test_client: TestClient
    ):
        """Walking off the end returns an empty page, not an error.

        A client paging through a feed that shrinks between requests must still
        get the pagination values it asked for, so it can tell where it is.
        """
        owner_id, headers = _signup(test_client, "notify_cov_page_end")
        _seed_feed(owner_id, ["CLM-COVA", "CLM-COVB"])

        response = test_client.get(
            "/api/v1/notifications", params={"limit": 2, "offset": 5}, headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["notifications"] == []
        assert body["count"] == 0
        assert body["limit"] == 2
        assert body["offset"] == 5

    def test_a_page_size_outside_the_bounds_is_rejected(self, test_client: TestClient):
        """The bounds are on the query, so a caller learns them as a 422."""
        _, headers = _signup(test_client, "notify_cov_bounds")

        over_long = test_client.get("/api/v1/notifications", params={"limit": 51}, headers=headers)
        empty_page = test_client.get("/api/v1/notifications", params={"limit": 0}, headers=headers)
        negative_offset = test_client.get(
            "/api/v1/notifications", params={"offset": -1}, headers=headers
        )

        assert over_long.status_code == 422
        assert empty_page.status_code == 422
        assert negative_offset.status_code == 422

    def test_the_upper_bound_page_size_is_accepted(self, test_client: TestClient):
        """The cap is inclusive, so a full page is not silently reduced."""
        owner_id, headers = _signup(test_client, "notify_cov_full_page")
        _seed_feed(owner_id, ["CLM-COVFULL"])

        response = test_client.get("/api/v1/notifications", params={"limit": 50}, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["limit"] == 50
        assert body["count"] == 1

    def test_the_offset_window_skips_the_newest_rows(self, test_client: TestClient):
        """The offset skips from the newest row, so page order is stable."""
        owner_id, headers = _signup(test_client, "notify_cov_window")
        _seed_feed(owner_id, ["CLM-COVW1", "CLM-COVW2", "CLM-COVW3"])

        first = test_client.get(
            "/api/v1/notifications", params={"limit": 1, "offset": 0}, headers=headers
        )
        second = test_client.get(
            "/api/v1/notifications", params={"limit": 1, "offset": 1}, headers=headers
        )

        assert [item["claim_id"] for item in first.json()["notifications"]] == ["CLM-COVW3"]
        assert [item["claim_id"] for item in second.json()["notifications"]] == ["CLM-COVW2"]
        assert first.json()["offset"] == 0
        assert second.json()["offset"] == 1


class TestUnreadFilter:
    """``unread_only`` is a filter on the caller's own feed, nothing more."""

    def test_an_all_read_feed_has_no_unread_page(self, test_client: TestClient):
        """Once every row is acknowledged the unread page is empty, not stale."""
        owner_id, headers = _signup(test_client, "notify_cov_all_read")
        feed = _seed_feed(owner_id, ["CLM-COVU1", "CLM-COVU2"])

        for notification_id in feed:
            marked = test_client.post(
                f"/api/v1/notifications/{notification_id}/read", headers=headers
            )
            assert marked.status_code == 200, marked.text

        full = test_client.get("/api/v1/notifications", headers=headers)
        unread = test_client.get(
            "/api/v1/notifications", params={"unread_only": "1"}, headers=headers
        )

        assert full.json()["count"] == 2
        assert unread.status_code == 200
        assert unread.json()["count"] == 0
        assert unread.json()["notifications"] == []

    def test_the_unread_filter_cannot_trim_another_users_feed(self, test_client: TestClient):
        """The owner predicate comes first, so the window cannot expose a foreign row."""
        owner_id, _ = _signup(test_client, "notify_cov_scoped_owner")
        _, intruder_headers = _signup(test_client, "notify_cov_scoped_intruder")
        _seed_feed(owner_id, ["CLM-COVS1"])

        response = test_client.get(
            "/api/v1/notifications", params={"unread_only": "1"}, headers=intruder_headers
        )

        assert response.status_code == 200
        assert response.json()["notifications"] == []
