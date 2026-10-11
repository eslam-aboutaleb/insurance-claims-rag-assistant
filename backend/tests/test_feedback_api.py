"""Tests for the answer feedback endpoints.

Every scenario drives the public API with a real bearer token. Conversations and
messages are seeded directly so a scenario can fix who owns which answer; the
autouse schema-reset fixture in ``tests/conftest.py`` wipes every table before
each test, so a seeded row is visible to exactly one test.

An unknown message id and a message belonging to another user are answered with
the same 403 on purpose, so the tests assert one status for both: the endpoint
must not confirm which message ids exist.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import create_access_token
from app.database import async_session_factory, engine
from app.models.answer_feedback import AnswerFeedback
from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage
from app.models.user import User

_TEST_PASSWORD = "FeedbackPass123!"  # noqa: S105 - fixture credential, not a secret

_COMMENT = "The answer cited a policy section that was superseded last year."
_OVERSIZED_COMMENT = "x" * 1001


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

    async def _run() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                for owner_id, message_id, content in rows:
                    conversation = Conversation(
                        id=uuid.uuid4(),
                        user_id=owner_id,
                        session_id=f"feedback-session-{message_id.hex}",
                        title="Feedback conversation",
                        messages=[],
                    )
                    session.add(conversation)
                    # The message is flushed only after its conversation exists:
                    # the two models share no relationship, so the unit of work
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

    asyncio.run(_run())


def _seed_user_and_answer(owner_id: uuid.UUID, message_id: uuid.UUID, content: str) -> None:
    """Insert a user that never signed up, plus one answer they own.

    Args:
        owner_id: The id the synthetic user is created with.
        message_id: The id the assistant answer is created with.
        content: The answer text.
    """

    async def _run() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                session.add(
                    User(
                        id=owner_id,
                        username=f"feedback_synthetic_{owner_id.hex[:8]}",
                        password_hash="unused-hash",  # noqa: S106 - fixture value, not a credential
                    )
                )
                # Postgres checks a foreign key per row, so the user is flushed
                # before the conversation that names it, not batched after.
                await session.flush()
                conversation = Conversation(
                    id=uuid.uuid4(),
                    user_id=owner_id,
                    session_id=f"feedback-session-{message_id.hex}",
                    title="Feedback conversation",
                    messages=[],
                )
                session.add(conversation)
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

    asyncio.run(_run())


def _feedback_rows(message_id: uuid.UUID) -> list[AnswerFeedback]:
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


def test_feedback_on_your_own_answer_persists(test_client: TestClient) -> None:
    """A rating on an answer the caller received is stored with its detail."""
    owner_id, headers = _signup(test_client, "feedback_owner_a")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Water damage is covered for sudden events.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={
            "message_id": str(message_id),
            "rating": "down",
            "comment": _COMMENT,
            "category": "outdated_policy",
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["rating"] == "down"
    assert body["message_id"] == str(message_id)
    assert body["comment"] == _COMMENT
    assert body["category"] == "outdated_policy"
    assert body["created_at"]

    rows = _feedback_rows(message_id)
    assert len(rows) == 1
    assert rows[0].user_id == owner_id
    assert rows[0].rating == "down"
    assert rows[0].comment == _COMMENT
    assert rows[0].category == "outdated_policy"


def test_a_rating_without_a_reason_is_stored(test_client: TestClient) -> None:
    """A bare thumbs-up needs neither a category nor a comment."""
    owner_id, headers = _signup(test_client, "feedback_owner_bare")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Your claim CLM-8821 is approved.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "up"},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["rating"] == "up"
    assert response.json()["comment"] is None
    assert response.json()["category"] is None


def test_changing_your_mind_revises_the_row(test_client: TestClient) -> None:
    """The unique constraint turns a second rating into an upsert, not a dupe."""
    owner_id, headers = _signup(test_client, "feedback_owner_revise")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "The deductible is $1,000.")])

    first = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "up"},
        headers=headers,
    )
    second = test_client.post(
        "/api/v1/feedback",
        json={
            "message_id": str(message_id),
            "rating": "down",
            "comment": _COMMENT,
            "category": "wrong_answer",
        },
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201

    rows = _feedback_rows(message_id)
    assert len(rows) == 1
    assert rows[0].rating == "down"
    assert rows[0].comment == _COMMENT
    assert rows[0].category == "wrong_answer"
    # The stored row is the one the response reported, not a fresh insert.
    assert rows[0].id == uuid.UUID(second.json()["id"])


def test_feedback_on_another_users_answer_is_forbidden(test_client: TestClient) -> None:
    """A user cannot rate an answer that was produced for someone else."""
    owner_id, _ = _signup(test_client, "feedback_owner_owner")
    _, intruder_headers = _signup(test_client, "feedback_owner_intruder")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Coverage begins on the policy start date.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "down"},
        headers=intruder_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert _feedback_rows(message_id) == []


def test_an_unknown_message_id_is_refused_like_a_foreign_one(test_client: TestClient) -> None:
    """A message that was never issued answers exactly like another user's."""
    owner_id, headers = _signup(test_client, "feedback_owner_unknown")
    own_message_id = uuid.uuid4()
    _seed_answers([(owner_id, own_message_id, "Flood damage is excluded.")])

    unknown = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(uuid.uuid4()), "rating": "down"},
        headers=headers,
    )

    assert unknown.status_code == 403
    assert unknown.json()["error"]["message"] == "This message is not available to you."
    # The refusal is indistinguishable from the one another user's answer earns.
    assert unknown.json()["error"]["code"] == "FORBIDDEN"


def test_a_rating_outside_the_enum_is_rejected(test_client: TestClient) -> None:
    """The rating is an enum, so free text never reaches the column."""
    owner_id, headers = _signup(test_client, "feedback_owner_bad_rating")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Towing is covered up to $100.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "sideways"},
        headers=headers,
    )

    assert response.status_code == 422


def test_a_category_outside_the_enum_is_rejected(test_client: TestClient) -> None:
    """The category is an enum too, so two rows stay comparable."""
    owner_id, headers = _signup(test_client, "feedback_owner_bad_category")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Rental reimbursement is $30 a day.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={
            "message_id": str(message_id),
            "rating": "down",
            "category": "the_model_was_mean",
        },
        headers=headers,
    )

    assert response.status_code == 422


def test_an_oversized_comment_is_rejected(test_client: TestClient) -> None:
    """The comment is capped, so a paste cannot become a stored document."""
    owner_id, headers = _signup(test_client, "feedback_owner_long")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Liability coverage follows the car.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={
            "message_id": str(message_id),
            "rating": "down",
            "comment": _OVERSIZED_COMMENT,
        },
        headers=headers,
    )

    assert response.status_code == 422


def test_feedback_requires_authentication(test_client: TestClient) -> None:
    """An anonymous caller cannot rate anything."""
    response = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(uuid.uuid4()), "rating": "up"},
    )

    assert response.status_code == 401


def test_withdrawing_removes_the_callers_rating(test_client: TestClient) -> None:
    """A withdrawn rating leaves the row gone, not merely updated."""
    owner_id, headers = _signup(test_client, "feedback_owner_withdraw")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Comprehensive covers theft.")])

    submitted = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "up"},
        headers=headers,
    )
    assert submitted.status_code == 201

    withdrawn = test_client.delete(f"/api/v1/feedback/{message_id}", headers=headers)

    assert withdrawn.status_code == 204
    assert _feedback_rows(message_id) == []


def test_withdrawing_a_rating_you_never_gave_is_a_noop(test_client: TestClient) -> None:
    """Nothing is left to say once a rating is gone, so the delete is idempotent."""
    owner_id, headers = _signup(test_client, "feedback_owner_noop")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Collision covers your own car.")])

    response = test_client.delete(f"/api/v1/feedback/{message_id}", headers=headers)

    assert response.status_code == 204
    assert _feedback_rows(message_id) == []


def test_withdrawing_leaves_an_audit_trail(test_client: TestClient) -> None:
    """A withdrawal is recorded even though the delete itself is idempotent.

    Once the row is gone the two states look identical, so the trail is the
    only thing that still distinguishes "never rated" from "withdrew a rating".
    """
    from app.models.audit_event import AuditEvent  # noqa: PLC0415

    owner_id, headers = _signup(test_client, "feedback_owner_audited")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Collision covers your own car.")])

    test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "up"},
        headers=headers,
    )
    assert test_client.delete(f"/api/v1/feedback/{message_id}", headers=headers).status_code == 204
    assert _feedback_rows(message_id) == []

    created = test_client.get(
        "/api/v1/admin/audit",
        params={"entity_type": "answer_feedback", "entity_id": str(message_id)},
        headers=headers,
    )
    assert created.status_code == 403, "the trail is admin-only"

    async def _actions() -> list[str]:
        await engine.dispose()
        async with async_session_factory() as session:
            rows = await session.execute(
                select(AuditEvent.action).where(
                    AuditEvent.entity_type == "answer_feedback",
                    AuditEvent.entity_id == str(message_id),
                )
            )
            return list(rows.scalars())

    actions = asyncio.run(_actions())
    # The action names are what make the pair readable without the row.
    assert "feedback.submitted" in actions
    assert "feedback.withdrawn" in actions


def test_withdrawing_someone_elses_rating_is_forbidden(test_client: TestClient) -> None:
    """The caller can only withdraw a rating on an answer they received."""
    owner_id, owner_headers = _signup(test_client, "feedback_owner_victim")
    _, intruder_headers = _signup(test_client, "feedback_owner_intruder_delete")
    message_id = uuid.uuid4()
    _seed_answers([(owner_id, message_id, "Gap coverage pays the difference.")])

    assert (
        test_client.post(
            "/api/v1/feedback",
            json={"message_id": str(message_id), "rating": "up"},
            headers=owner_headers,
        ).status_code
        == 201
    )

    response = test_client.delete(f"/api/v1/feedback/{message_id}", headers=intruder_headers)

    assert response.status_code == 403
    # The intruder's refusal removed nothing: the owner's rating still stands.
    assert len(_feedback_rows(message_id)) == 1


def test_withdrawing_requires_authentication(test_client: TestClient) -> None:
    """An anonymous caller cannot withdraw anything."""
    response = test_client.delete(f"/api/v1/feedback/{uuid.uuid4()}")

    assert response.status_code == 401


def test_feedback_endpoints_work_with_only_the_feedback_router_mounted() -> None:
    """The endpoints are self-contained, not dependent on the v1 router registration.

    Mounting the feedback router on a bare ``FastAPI`` app proves the endpoints
    need no middleware, no sibling router, and no startup hook beyond the
    dependencies they declare.
    """
    from fastapi import FastAPI  # noqa: PLC0415

    from app.api.v1.feedback import router as feedback_router  # noqa: PLC0415

    owner_id = uuid.uuid4()
    message_id = uuid.uuid4()
    _seed_user_and_answer(owner_id, message_id, "Roadside assistance is an add-on.")

    standalone_app = FastAPI()
    standalone_app.include_router(feedback_router, prefix="/api/v1/feedback")
    headers = {"Authorization": f"Bearer {create_access_token(owner_id)}"}

    with TestClient(standalone_app) as client:
        submitted = client.post(
            "/api/v1/feedback",
            json={"message_id": str(message_id), "rating": "up"},
            headers=headers,
        )
        withdrawn = client.delete(f"/api/v1/feedback/{message_id}", headers=headers)
        anonymous = client.post(
            "/api/v1/feedback",
            json={"message_id": str(message_id), "rating": "up"},
        )

    assert submitted.status_code == 201
    assert withdrawn.status_code == 204
    assert anonymous.status_code == 401
