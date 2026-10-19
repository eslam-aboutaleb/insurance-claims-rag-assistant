"""Admin namespace, RBAC gating, and audit-trail integration tests.

Covers the authorization boundary (every admin route refuses a policyholder and
serves an admin), the evaluation gate that used to be open to every
policyholder, the claim-lifecycle audit rows and their exactly-once guarantee,
the audit trail surviving its own failure, the paged and unscoped audit
listing, and the instrumentation of the high-value actions.

The ``audit_events`` table is not in the suite's per-test reset list, so every
audit assertion filters on a unique entity id rather than assuming an empty
table: a trail that accumulated yesterday's entries is exactly the behaviour
under test, not a nuisance.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1.admin import router as admin_router
from app.audit import record_audit
from app.core.database import async_session_factory, engine
from app.models.conversations.answer_feedback import AnswerFeedback
from app.models.platform.audit_event import AuditEvent
from app.models.claims.claim import Claim
from app.models.conversations.conversation import Conversation
from app.models.conversations.conversation_message import ConversationMessage
from app.models.claims.embedding_job import EmbeddingJob
from app.models.user import User

_PASSWORD = "TestPass123!"
_POLICYHOLDER = "policyholder"
_ADMIN = "admin"
_RETRIEVAL_ONLY = "retrieval-only"


def _unique(prefix: str) -> str:
    """Build a collision-free identifier for one test run.

    Args:
        prefix: A short label identifying what the identifier names.

    Returns:
        str: ``<prefix>_<random hex>``, unique enough to isolate audit rows and
        usernames across every other test in the session.
    """
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _signup(client: TestClient, username: str, *, role: str = _POLICYHOLDER) -> dict[str, Any]:
    """Register an account and return its id, username, and auth headers.

    This is the suite's user fixture factory. The optional ``role`` parameter
    promotes the account after signup, because no public route assigns a role;
    callers that omit it keep exactly the behaviour they had before the
    parameter existed -- a plain policyholder.

    Args:
        client: The test client to sign up through.
        username: The username to register.
        role: The role to grant the account after signup.

    Returns:
        dict[str, Any]: The account's ``user_id``, ``username``, and the
        ``headers`` carrying its bearer token.
    """
    response = client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": _PASSWORD},
    )
    assert response.status_code == 201
    body = response.json()
    if role != _POLICYHOLDER:
        _promote(body["user_id"], role)
    return {
        "user_id": body["user_id"],
        "username": username,
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


def _promote(user_id: str, role: str) -> None:
    """Grant a role by writing the column directly.

    Signup can only mint a policyholder and no endpoint assigns a role, so a
    test that needs an operator promotes the row the way an operator would.

    Args:
        user_id: The account to promote.
        role: The role to grant, one of the ``UserRole`` values.
    """

    async def _run() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                user = await session.get(User, uuid.UUID(user_id))
                assert user is not None
                user.role = role
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _run_db(work: Any) -> Any:
    """Run one async unit of work against the test database from a sync test.

    Each request the TestClient makes runs on its own portal loop while this
    runs on a fresh ``asyncio.run`` loop, and the engine is NullPooled in tests,
    so the connection never crosses loops. This is the same pattern the rest of
    the suite uses for direct database work.

    Args:
        work: An async callable taking the session to work in.

    Returns:
        Any: Whatever ``work`` returns.
    """

    async def _run() -> Any:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _audit_rows(
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
) -> list[AuditEvent]:
    """Read audit rows matching the given filters.

    Args:
        entity_type: Optional entity-kind filter.
        entity_id: Optional entity filter.
        actor_id: Optional actor filter.
        action: Optional action filter.

    Returns:
        list[AuditEvent]: The matching rows, oldest first.
    """

    async def _work(session: Any) -> list[AuditEvent]:
        stmt = select(AuditEvent)
        if entity_type is not None:
            stmt = stmt.where(AuditEvent.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditEvent.entity_id == entity_id)
        if actor_id is not None:
            stmt = stmt.where(AuditEvent.actor_id == uuid.UUID(actor_id))
        if action is not None:
            stmt = stmt.where(AuditEvent.action == action)
        stmt = stmt.order_by(AuditEvent.created_at, AuditEvent.id)
        return list((await session.execute(stmt)).scalars().all())

    return _run_db(_work)


def _seed(rows: list[Any]) -> None:
    """Persist ORM rows the read endpoints have nothing to scope over.

    Args:
        rows: The ORM instances to insert.
    """

    async def _work(session: Any) -> None:
        session.add_all(rows)
        await session.commit()

    _run_db(_work)


def _claim_row(
    owner_id: str,
    claim_id: str,
    *,
    status: str = "Submitted",
) -> Claim:
    """Build one claim row for seeding.

    Args:
        owner_id: The user the claim belongs to.
        claim_id: The human-readable claim identifier.
        status: The claim status to store.

    Returns:
        Claim: An unsaved claim ORM instance.
    """
    return Claim(
        claim_id=claim_id,
        policy_number=f"POL-{claim_id[-4:]}",
        claim_type="Water Damage",
        status=status,
        amount=Decimal("1500.00"),
        description=f"Factual description for {claim_id}.",
        owner_id=uuid.UUID(owner_id),
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def _job_row(owner_id: str, *, claim_id: str, status: str, retry_count: int) -> EmbeddingJob:
    """Build one embedding-outbox job row for seeding.

    Args:
        owner_id: The claim owner the job belongs to.
        claim_id: The claim identifier the job embeds.
        status: The job status to store.
        retry_count: The retry count to store.

    Returns:
        EmbeddingJob: An unsaved job ORM instance.
    """
    return EmbeddingJob(
        claim_id=claim_id,
        claim_uuid=uuid.uuid4(),
        owner_id=uuid.UUID(owner_id),
        claim_type="Water Damage",
        description="Factual description for an embed job.",
        policy_number="POL-ADMINQ",
        claim_status="Submitted",
        status=status,
        retry_count=retry_count,
        status_detail="timeout while embedding" if status == "dead_letter" else None,
    )


def _seed_answers(rows: list[tuple[str, uuid.UUID, str]]) -> None:
    """Persist one conversation plus one assistant message per tuple.

    Args:
        rows: ``(owner_id, message_id, content)`` triples, one assistant answer
            each, so a feedback row can point at a message the owner received.
    """

    async def _work(session: Any) -> None:
        for owner_id, message_id, content in rows:
            conversation = Conversation(
                user_id=uuid.UUID(owner_id),
                session_id=_unique("conversation"),
            )
            session.add(conversation)
            await session.flush()
            session.add(
                ConversationMessage(
                    id=message_id,
                    conversation_id=conversation.id,
                    role="assistant",
                    content=content,
                )
            )
        await session.commit()

    _run_db(_work)


def _seed_audit_rows(count: int, entity_type: str) -> list[str]:
    """Write audit rows directly, the way an operator-free test can control them.

    Args:
        count: How many rows to write.
        entity_type: The entity type to tag them with, chosen unique per call so
            filtering on it isolates exactly these rows.

    Returns:
        list[str]: The entity ids that were written, in order.
    """
    entity_ids: list[str] = []

    async def _work(session: Any) -> None:
        for index in range(count):
            entity_id = f"{entity_type}-{index:02d}"
            entity_ids.append(entity_id)
            await record_audit(
                session, "test.seeded", entity_type, entity_id, after={"index": index}
            )
            # ``record_audit`` persists its row with the caller's commit, so the
            # caller commits.
            await session.commit()

    _run_db(_work)
    return entity_ids


def _admin_routes() -> list[str]:
    """Every admin GET path, read from the router itself.

    Reading the routes rather than listing them in a table is what keeps a new
    admin path from existing without coverage: adding a route adds it here. Each
    route path already carries the router's ``/admin`` prefix, so the full URL
    is the versioned prefix plus that.

    Returns:
        list[str]: The full URL paths the admin router serves with GET.
    """
    return sorted(
        f"/api/v1{route.path}"
        for route in admin_router.routes
        if "GET" in getattr(route, "methods", set())
    )


@pytest.mark.parametrize("path", _admin_routes())
def test_every_admin_route_refuses_a_policyholder(test_client: TestClient, path: str) -> None:
    """A policyholder is refused by every admin route with the 403 envelope."""
    holder = _signup(test_client, _unique("holder"))

    response = test_client.get(path, headers=holder["headers"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("path", _admin_routes())
def test_every_admin_route_serves_an_admin(test_client: TestClient, path: str) -> None:
    """An admin is served by every admin route, empty tables included."""
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)

    response = test_client.get(path, headers=admin["headers"])

    assert response.status_code == 200


def test_rag_evaluation_refuses_a_policyholder(test_client: TestClient) -> None:
    """The evaluation harness is the admin-grade endpoint this boundary closes."""
    holder = _signup(test_client, _unique("holder"))

    response = test_client.post(
        "/api/v1/rag/evaluate",
        json={"mode": _RETRIEVAL_ONLY},
        headers=holder["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_rag_evaluation_admits_an_admin(test_client: TestClient) -> None:
    """An admin still reaches the evaluation harness the policyholder is refused."""
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)
    test_client.app.state.limiter.enabled = False
    try:
        with patch(
            "app.domain.evaluation.retrieve_hybrid",
            new_callable=AsyncMock,
            return_value={"results": []},
        ):
            response = test_client.post(
                "/api/v1/rag/evaluate",
                json={"mode": _RETRIEVAL_ONLY},
                headers=admin["headers"],
            )
    finally:
        test_client.app.state.limiter.enabled = True

    assert response.status_code == 200
    assert response.json()["config"]["mode"] == _RETRIEVAL_ONLY
    assert "retrieval_metrics" in response.json()


def test_admin_claims_queue_is_cross_owner_and_filterable(test_client: TestClient) -> None:
    """The admin queue spans owners and narrows on status and owner filters."""
    first = _signup(test_client, _unique("holder_a"))
    second = _signup(test_client, _unique("holder_b"))
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)
    _seed(
        [
            _claim_row(first["user_id"], "CLM-A1"),
            _claim_row(first["user_id"], "CLM-A2", status="Under Review"),
            _claim_row(second["user_id"], "CLM-B1"),
        ]
    )

    unfiltered = test_client.get("/api/v1/admin/claims", headers=admin["headers"])
    assert unfiltered.status_code == 200
    body = unfiltered.json()
    assert {claim["claim_id"] for claim in body["claims"]} == {"CLM-A1", "CLM-A2", "CLM-B1"}
    assert body["count"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert set(body["claims"][0]) == {
        "claim_id",
        "policy_number",
        "claim_type",
        "status",
        "amount",
        "owner_id",
        "created_at",
    }

    by_owner = test_client.get(
        f"/api/v1/admin/claims?owner_id={first['user_id']}",
        headers=admin["headers"],
    )
    assert by_owner.status_code == 200
    assert {claim["claim_id"] for claim in by_owner.json()["claims"]} == {"CLM-A1", "CLM-A2"}

    by_status = test_client.get(
        "/api/v1/admin/claims?status=Under%20Review",
        headers=admin["headers"],
    )
    assert by_status.status_code == 200
    assert {claim["claim_id"] for claim in by_status.json()["claims"]} == {"CLM-A2"}


def test_admin_embedding_jobs_report_depth_and_dead_letters(test_client: TestClient) -> None:
    """The outbox view reports queue depth per status and the dead-letter list."""
    holder = _signup(test_client, _unique("holder"))
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)
    _seed(
        [
            _job_row(holder["user_id"], claim_id="CLM-Q1", status="pending", retry_count=0),
            _job_row(holder["user_id"], claim_id="CLM-Q2", status="pending", retry_count=1),
            _job_row(holder["user_id"], claim_id="CLM-Q3", status="failed", retry_count=3),
            _job_row(holder["user_id"], claim_id="CLM-Q4", status="dead_letter", retry_count=4),
        ]
    )

    response = test_client.get("/api/v1/admin/embedding-jobs", headers=admin["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"pending": 2, "failed": 1, "dead_letter": 1}
    assert body["count"] == 4
    assert len(body["dead_letter"]) == 1
    dead_letter = body["dead_letter"][0]
    assert dead_letter["claim_id"] == "CLM-Q4"
    assert dead_letter["retry_count"] == 4
    assert dead_letter["status_detail"] == "timeout while embedding"

    filtered = test_client.get(
        "/api/v1/admin/embedding-jobs?status=failed",
        headers=admin["headers"],
    )
    assert filtered.status_code == 200
    # The depth summary is of the whole queue, so a filtered page still shows
    # where the rest of the outbox stands.
    assert filtered.json()["counts"] == {"pending": 2, "failed": 1, "dead_letter": 1}
    assert {job["claim_id"] for job in filtered.json()["jobs"]} == {"CLM-Q3"}


def test_claim_withdrawal_writes_exactly_one_audit_row(test_client: TestClient) -> None:
    """A status transition leaves exactly one trail row with before and after."""
    holder = _signup(test_client, _unique("holder"))
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    _seed([_claim_row(holder["user_id"], claim_id)])

    response = test_client.post(
        f"/api/v1/claims/{claim_id}/status",
        json={"status": "Withdrawn"},
        headers=holder["headers"],
    )

    assert response.status_code == 200
    rows = _audit_rows(entity_type="claim", entity_id=claim_id)
    assert len(rows) == 1
    assert rows[0].action == "claim.withdrawn"
    assert rows[0].before == {"status": "Submitted"}
    assert rows[0].after == {"status": "Withdrawn"}
    assert rows[0].actor_id == uuid.UUID(holder["user_id"])


def test_a_failed_audit_write_does_not_break_the_transition(test_client: TestClient) -> None:
    """A trail that cannot be written leaves the business action intact."""
    holder = _signup(test_client, _unique("holder"))
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    _seed([_claim_row(holder["user_id"], claim_id)])

    with patch(
        "app.audit.service.AuditEvent", MagicMock(side_effect=RuntimeError("trail offline"))
    ):
        response = test_client.post(
            f"/api/v1/claims/{claim_id}/status",
            json={"status": "Withdrawn"},
            headers=holder["headers"],
        )

    assert response.status_code == 200
    assert response.json()["status"] == "Withdrawn"
    assert _audit_rows(entity_type="claim", entity_id=claim_id) == []


class _BrokenSession:
    """A session whose every write fails the way a dead database does."""

    def add(self, *_args: object, **_kwargs: object) -> None:
        """Accept an add without doing anything, as a session would."""

    def begin_nested(self) -> None:
        """Refuse to open the savepoint the trail write needs.

        Raises:
            RuntimeError: Always, standing in for a lost connection.
        """
        raise RuntimeError("connection refused")

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        """Fail the write.

        Raises:
            RuntimeError: Always, standing in for a lost connection.
        """
        raise RuntimeError("connection refused")

    async def commit(self) -> None:
        """Fail the commit.

        Raises:
            RuntimeError: Always, standing in for a lost connection.
        """
        raise RuntimeError("connection refused")

    async def rollback(self) -> None:
        """Succeed, so the caller's session is left usable."""


def test_record_audit_swallows_a_failed_write(caplog: pytest.LogCaptureFixture) -> None:
    """``record_audit`` reports the failure and never raises into the caller."""

    async def _run() -> None:
        await record_audit(_BrokenSession(), "auth.signin", "user", "user-uuid")

    with caplog.at_level(logging.WARNING, logger="app.audit.service"):
        result = asyncio.run(_run())

    assert result is None
    assert "could not be written" in caplog.text


def test_admin_audit_listing_is_paged(test_client: TestClient) -> None:
    """The trail is paged, and the pages partition the result without repeats."""
    marker = _unique("trail")
    expected = set(_seed_audit_rows(5, marker))
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)

    seen: set[str] = set()
    for offset, expected_count in ((0, 2), (2, 2), (4, 1), (6, 0)):
        response = test_client.get(
            f"/api/v1/admin/audit?entity_type={marker}&limit=2&offset={offset}",
            headers=admin["headers"],
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == expected_count
        assert body["limit"] == 2
        assert body["offset"] == offset
        page_ids = {entry["entity_id"] for entry in body["events"]}
        assert len(page_ids) == len(body["events"])
        assert not seen & page_ids
        seen |= page_ids

    assert seen == expected


def test_admin_audit_listing_is_not_owner_scoped(test_client: TestClient) -> None:
    """An admin reads another account's signup, which no policyholder could."""
    subject = _signup(test_client, _unique("subject"))
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)

    response = test_client.get(
        f"/api/v1/admin/audit?entity_type=user&entity_id={subject['user_id']}",
        headers=admin["headers"],
    )

    assert response.status_code == 200
    entries = response.json()["events"]
    assert len(entries) == 1
    assert entries[0]["action"] == "auth.signup"
    assert entries[0]["actor_id"] == subject["user_id"]
    assert entries[0]["after"] == {"username": subject["username"]}


def test_auth_events_are_audited(test_client: TestClient) -> None:
    """Signup, sign-in, failed sign-in, and logout each leave a trail row."""
    holder = _signup(test_client, _unique("holder"))

    signin = test_client.post(
        "/api/v1/auth/signin",
        json={"username": holder["username"], "password": _PASSWORD},
    )
    assert signin.status_code == 200

    failed = test_client.post(
        "/api/v1/auth/signin",
        json={"username": holder["username"], "password": "WrongPassword123!"},
    )
    assert failed.status_code == 401

    logout = test_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204

    # The signup and sign-in entries are keyed on the account they concern; the
    # failed attempt is keyed on the username it attempted, because a failed
    # attempt has no account to point at.
    account_rows = _audit_rows(entity_type="user", entity_id=holder["user_id"])
    assert {row.action for row in account_rows} == {"auth.signup", "auth.signin"}
    signup_row = next(row for row in account_rows if row.action == "auth.signup")
    assert signup_row.actor_id == uuid.UUID(holder["user_id"])

    failed_rows = _audit_rows(
        entity_type="user",
        entity_id=holder["username"],
        action="auth.signin_failed",
    )
    assert len(failed_rows) == 1
    # The failure is unattributed on purpose: crediting it to the account whose
    # name was guessed would make a wrong password look like that user's act.
    assert failed_rows[0].actor_id is None

    session_rows = _audit_rows(entity_type="session")
    assert any(row.action == "auth.logout" for row in session_rows)


def test_feedback_submission_is_audited(test_client: TestClient) -> None:
    """A rating on a real answer reaches the trail an operator alone can read."""
    holder = _signup(test_client, _unique("holder"))
    message_id = uuid.uuid4()
    _seed_answers([(holder["user_id"], message_id, "Water damage is covered for sudden events.")])

    response = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(message_id), "rating": "down", "category": "wrong_answer"},
        headers=holder["headers"],
    )

    assert response.status_code == 201
    rows = _audit_rows(entity_type="answer_feedback", entity_id=str(message_id))
    assert len(rows) == 1
    assert rows[0].action == "feedback.submitted"
    assert rows[0].after == {
        "rating": "down",
        "category": "wrong_answer",
        "comment": None,
    }
    assert rows[0].actor_id == uuid.UUID(holder["user_id"])


def test_admin_feedback_feed_lists_down_rated_answers(test_client: TestClient) -> None:
    """The feed carries the down-rated answers with their text and their owner."""
    first = _signup(test_client, _unique("holder_a"))
    second = _signup(test_client, _unique("holder_b"))
    admin = _signup(test_client, _unique("admin"), role=_ADMIN)
    good_id = uuid.uuid4()
    wrong_id = uuid.uuid4()
    other_id = uuid.uuid4()
    _seed_answers(
        [
            (first["user_id"], good_id, "The deductible is $1,000."),
            (second["user_id"], wrong_id, "Flood damage is excluded."),
            (first["user_id"], other_id, "Towing is covered up to $100."),
        ]
    )
    _seed(
        [
            AnswerFeedback(
                message_id=good_id,
                user_id=uuid.UUID(first["user_id"]),
                rating="down",
                category="wrong_answer",
                comment="The deductible is $2,000 on this policy.",
            ),
            AnswerFeedback(
                message_id=wrong_id,
                user_id=uuid.UUID(second["user_id"]),
                rating="up",
            ),
            AnswerFeedback(
                message_id=other_id,
                user_id=uuid.UUID(first["user_id"]),
                rating="down",
            ),
        ]
    )

    response = test_client.get("/api/v1/admin/feedback", headers=admin["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {item["answer"] for item in body["feedback"]} == {
        "The deductible is $1,000.",
        "Towing is covered up to $100.",
    }
    detailed = next(
        item for item in body["feedback"] if item["answer"] == "The deductible is $1,000."
    )
    assert detailed["username"] == first["username"]
    assert detailed["user_id"] == first["user_id"]
    assert detailed["message_id"] == str(good_id)
    assert detailed["category"] == "wrong_answer"
    assert detailed["comment"] == "The deductible is $2,000 on this policy."
    assert set(detailed) == {
        "id",
        "message_id",
        "user_id",
        "username",
        "category",
        "comment",
        "created_at",
        "answer",
    }
