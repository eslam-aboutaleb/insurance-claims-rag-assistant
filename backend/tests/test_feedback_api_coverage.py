"""Coverage tests for the error branches of ``app.api.v1.feedback``.

The happy paths live in ``tests/test_feedback_api.py``; what this file pins are
the branches that only a failure reaches: a caller whose id is not a UUID, a
message that was never issued, the audit row a withdrawal leaves behind, and the
edges of the two optional fields.

Every scenario drives the public API with a real bearer token against the
isolated test database, so a seeded row is visible to exactly one test.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import get_current_user
from app.database import async_session_factory, engine
from app.models.answer_feedback import AnswerFeedback
from app.models.audit_event import AuditEvent
from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage

_TEST_PASSWORD = "FeedbackCoverage123!"  # noqa: S105 - fixture credential, not a secret
_MAX_COMMENT_LENGTH = 1000


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


def _seed_answers(rows: list[tuple[uuid.UUID, uuid.UUID, str]]) -> None:
    """Persist conversation and message rows for users that already exist.

    Args:
        rows: One ``(owner_id, message_id, content)`` tuple per answer. Each
            answer gets its own conversation, so answers in one test can belong
            to different users.
    """

    async def _seed() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                for owner_id, message_id, content in rows:
                    conversation = Conversation(
                        id=uuid.uuid4(),
                        user_id=owner_id,
                        session_id=f"coverage-session-{message_id.hex}",
                        title="Coverage conversation",
                        messages=[],
                    )
                    session.add(conversation)
                    # The message is flushed only after its conversation exists:
                    # the models declare no relationship, so the unit of work
                    # would otherwise emit the insert first and trip the FK.
                    await session.flush()
                    session.add(
                        ConversationMessage(
                            id=message_id,
                            conversation_id=conversation.id,
                            role="assistant",
                            content=content,
                            timestamp=datetime(2026, 3, 2, tzinfo=UTC),
                        )
                    )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())


def _rating_rows(message_id: uuid.UUID) -> list[AnswerFeedback]:
    """Read back every rating stored against one message.

    Args:
        message_id: The message whose ratings are read.

    Returns:
        list[AnswerFeedback]: The stored rows, in the order they were written.
    """

    async def _run() -> list[AnswerFeedback]:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                return list(
                    (
                        await session.execute(
                            select(AnswerFeedback)
                            .where(AnswerFeedback.message_id == message_id)
                            .order_by(AnswerFeedback.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _audit_rows(message_id: uuid.UUID) -> list[AuditEvent]:
    """Read every audit entry written for one rated message.

    Args:
        message_id: The message whose audit entries are read.

    Returns:
        list[AuditEvent]: The entries, oldest first.
    """

    async def _run() -> list[AuditEvent]:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                return list(
                    (
                        await session.execute(
                            select(AuditEvent)
                            .where(
                                AuditEvent.entity_type == "answer_feedback",
                                AuditEvent.entity_id == str(message_id),
                            )
                            .order_by(AuditEvent.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _with_invalid_user_id(client: TestClient) -> None:
    """Override authentication so the caller's id is not a valid UUID.

    Args:
        client: The client the override applies to.
    """

    async def _override() -> str:
        return "not-a-valid-uuid"

    from app.main import app  # noqa: PLC0415

    app.dependency_overrides[get_current_user] = _override


class TestInvalidUserId:
    """An authenticated id that is not a UUID is a 401, not a crash."""

    def test_submitting_with_a_non_uuid_user_id_is_rejected(self, test_client: TestClient):
        """The endpoint refuses a caller it cannot parse rather than guessing."""
        _with_invalid_user_id(test_client)
        try:
            response = test_client.post(
                "/api/v1/feedback",
                json={"message_id": str(uuid.uuid4()), "rating": "up"},
            )
        finally:
            test_client.app.dependency_overrides.pop(get_current_user, None)

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid user"

    def test_withdrawing_with_a_non_uuid_user_id_is_rejected(self, test_client: TestClient):
        """The withdrawal path parses the caller's id the same way."""
        _with_invalid_user_id(test_client)
        message_id = uuid.uuid4()
        try:
            response = test_client.delete(f"/api/v1/feedback/{message_id}")
        finally:
            test_client.app.dependency_overrides.pop(get_current_user, None)

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid user"


class TestWithdrawalAudit:
    """Withdrawing leaves an append-only row behind the delete."""

    def test_the_withdrawal_row_records_what_was_reversed(self, test_client: TestClient):
        """The row names the action, the message, the actor, and what was taken back.

        Once the rating row is gone, "never rated it" and "withdrew a rating"
        look identical, so this entry is the only thing that still tells them
        apart. Its fields are what a reader needs to tell the two apart.
        """
        owner_id, headers = _signup(test_client, "feedback_coverage_audit")
        message_id = uuid.uuid4()
        _seed_answers([(owner_id, message_id, "Comprehensive covers theft.")])

        assert (
            test_client.post(
                "/api/v1/feedback",
                json={"message_id": str(message_id), "rating": "up"},
                headers=headers,
            ).status_code
            == 201
        )
        assert (
            test_client.delete(f"/api/v1/feedback/{message_id}", headers=headers).status_code == 204
        )

        entries = _audit_rows(message_id)
        assert [entry.action for entry in entries] == [
            "feedback.submitted",
            "feedback.withdrawn",
        ]
        withdrawal = entries[-1]
        assert withdrawal.entity_type == "answer_feedback"
        assert withdrawal.entity_id == str(message_id)
        assert withdrawal.actor_id == owner_id
        assert withdrawal.before == {"message_id": str(message_id)}
        # The submitted entry is the one that carries the rating it stored.
        assert entries[0].after is not None
        assert entries[0].after["rating"] == "up"

    def test_withdrawing_a_rating_you_never_gave_still_records_the_audit_row(
        self, test_client: TestClient
    ):
        """The delete is idempotent, but the trail is not silent about it."""
        owner_id, headers = _signup(test_client, "feedback_coverage_audit_noop")
        message_id = uuid.uuid4()
        _seed_answers([(owner_id, message_id, "Collision covers your own car.")])

        assert (
            test_client.delete(f"/api/v1/feedback/{message_id}", headers=headers).status_code == 204
        )
        assert _rating_rows(message_id) == []

        entries = _audit_rows(message_id)
        assert [entry.action for entry in entries] == ["feedback.withdrawn"]

    def test_withdrawing_a_message_that_does_not_exist_writes_no_audit_row(
        self, test_client: TestClient
    ):
        """A refused withdrawal must not leave a trail entry behind.

        Writing the row before the ownership check would let anyone append to
        the trail for a message that was never theirs.
        """
        owner_id, headers = _signup(test_client, "feedback_coverage_audit_foreign")
        message_id = uuid.uuid4()
        _seed_answers([(owner_id, message_id, "Gap coverage pays the difference.")])

        response = test_client.delete(f"/api/v1/feedback/{uuid.uuid4()}", headers=headers)

        assert response.status_code == 403
        assert response.json()["error"]["message"] == "This message is not available to you."
        assert _audit_rows(message_id) == []


class TestOptionalFieldBoundaries:
    """The two optional fields have edges that are worth pinning."""

    def test_a_comment_at_the_length_boundary_is_stored_whole(self, test_client: TestClient):
        """The cap is 1000 characters, and the last one of them is kept.

        A limit that is off by one silently truncates a user's explanation, and
        the stored text is the only copy of it.
        """
        owner_id, headers = _signup(test_client, "feedback_coverage_boundary")
        message_id = uuid.uuid4()
        _seed_answers([(owner_id, message_id, "Roadside assistance is an add-on.")])
        comment = "x" * _MAX_COMMENT_LENGTH

        response = test_client.post(
            "/api/v1/feedback",
            json={
                "message_id": str(message_id),
                "rating": "down",
                "comment": comment,
                "category": "other",
            },
            headers=headers,
        )

        assert response.status_code == 201, response.text
        assert response.json()["comment"] == comment
        rows = _rating_rows(message_id)
        assert len(rows) == 1
        assert rows[0].comment == comment
        assert rows[0].category == "other"

    def test_a_declined_reason_keeps_the_structured_category(self, test_client: TestClient):
        """A structured reason without any free text is stored on its own.

        The two fields are independent, so picking a category must not require
        the comment that goes with it in the UI.
        """
        owner_id, headers = _signup(test_client, "feedback_coverage_reason")
        message_id = uuid.uuid4()
        _seed_answers([(owner_id, message_id, "Rental reimbursement is $30 a day.")])

        response = test_client.post(
            "/api/v1/feedback",
            json={
                "message_id": str(message_id),
                "rating": "down",
                "comment": None,
                "category": "missing_info",
            },
            headers=headers,
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["category"] == "missing_info"
        assert body["comment"] is None
        rows = _rating_rows(message_id)
        assert rows[0].category == "missing_info"
        assert rows[0].comment is None
