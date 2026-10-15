"""Tests for the claim lifecycle: transitions, history, and submission cancellation.

Every scenario drives the public API with a real bearer token. Claim rows are
seeded directly so a scenario can fix the status a claim starts from; the
autouse schema-reset fixture in ``tests/conftest.py`` wipes every table before
each test, so a seeded row is visible to exactly one test. The ``claim_events``
table needs no entry in that reset: the foreign key to ``claims`` cascades, so
deleting the claim removes its history.

Administrative transitions are refused to a policyholder, so the scenarios that
drive them borrow the interim authority seam the endpoint exposes. Tests that
must prove the refusal runs without it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.v1 import claims as claims_api
from app.core.database import async_session_factory, engine
from app.models.claims.claim import Claim
from app.models.claims.claim_event import ALLOWED_STATUS_TRANSITIONS, ClaimStatus
from app.models.claims.embedding_job import EmbeddingJob

_TEST_PASSWORD = "LifecyclePass123!"  # noqa: S105 - fixture credential, not a secret

_VALID_SUBMISSION = {
    "policy_number": "POL-4417",
    "claim_type": "Water Damage",
    "amount": 1875.25,
    "description": "A frozen pipe burst flooded the basement overnight.",
}

# One case per row of the transition map, so a status the map forgets to reach
# is missing from this list rather than silently untested.
_ALLOWED_TRANSITIONS = [
    pytest.param(source, target, id=f"{source.value}->{target.value}")
    for source, targets in ALLOWED_STATUS_TRANSITIONS.items()
    for target in sorted(targets)
]

_ILLEGAL_TRANSITIONS = [
    pytest.param(ClaimStatus.SUBMITTED, ClaimStatus.APPROVED, id="Submitted->Approved"),
    pytest.param(ClaimStatus.SUBMITTED, ClaimStatus.INFO_REQUESTED, id="Submitted->InfoRequested"),
    pytest.param(ClaimStatus.SUBMITTED, ClaimStatus.CLOSED, id="Submitted->Closed"),
    pytest.param(ClaimStatus.UNDER_REVIEW, ClaimStatus.SUBMITTED, id="UnderReview->Submitted"),
    pytest.param(ClaimStatus.UNDER_REVIEW, ClaimStatus.CLOSED, id="UnderReview->Closed"),
    pytest.param(ClaimStatus.INFO_REQUESTED, ClaimStatus.APPROVED, id="InfoRequested->Approved"),
    pytest.param(ClaimStatus.INFO_REQUESTED, ClaimStatus.WITHDRAWN, id="InfoRequested->Withdrawn"),
    pytest.param(ClaimStatus.APPROVED, ClaimStatus.DENIED, id="Approved->Denied"),
    pytest.param(ClaimStatus.APPROVED, ClaimStatus.CLOSED, id="Approved->Closed"),
    pytest.param(ClaimStatus.DENIED, ClaimStatus.CLOSED, id="Denied->Closed"),
    pytest.param(ClaimStatus.WITHDRAWN, ClaimStatus.UNDER_REVIEW, id="Withdrawn->UnderReview"),
    pytest.param(ClaimStatus.CLOSED, ClaimStatus.UNDER_REVIEW, id="Closed->UnderReview"),
]


@pytest.fixture
def as_claims_handler(monkeypatch: pytest.MonkeyPatch):
    """Grant one caller the endpoint's interim administrative authority.

    The endpoint cannot prove a caller is a claims handler until an
    authorization model exists, so this widens the seam the endpoint documents
    for the credential the request already carries.

    Args:
        monkeypatch: Pytest's monkeypatch fixture, which restores the seam when
            the test ends.

    Returns:
        Callable[[uuid.UUID], None]: A function granting the authority to one
            user.
    """

    def _grant(user_id: uuid.UUID) -> None:
        granted = set(claims_api.ADMINISTRATIVE_TRANSITION_ACTORS)
        granted.add(str(user_id))
        monkeypatch.setattr(claims_api, "ADMINISTRATIVE_TRANSITION_ACTORS", frozenset(granted))

    return _grant


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


def _claim_row(  # noqa: PLR0913
    owner_id: uuid.UUID,
    claim_id: str,
    *,
    status: str = "Submitted",
    claim_type: str = "Water Damage",
    amount: str = "1500.00",
    created_at: datetime,
) -> Claim:
    """Build one claim row for seeding.

    Args:
        owner_id: The user the claim belongs to.
        claim_id: The human-readable claim identifier.
        status: Claim status to start the claim from.
        claim_type: Claim category to store.
        amount: Claimed amount, stored exactly as ``Numeric(12, 2)`` would keep it.
        created_at: Filing timestamp, so ordering is deterministic per test.

    Returns:
        Claim: An unsaved claim ORM instance.
    """
    return Claim(
        claim_id=claim_id,
        policy_number=f"POL-{claim_id[-4:]}",
        claim_type=claim_type,
        status=status,
        amount=Decimal(amount),
        description=f"Factual description for {claim_id}.",
        owner_id=owner_id,
        created_at=created_at,
    )


def _seed_claims(rows: list[Claim]) -> None:
    """Persist claim rows so the lifecycle endpoints have claims to move.

    Args:
        rows: Claim ORM instances to insert.
    """

    async def _run() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                session.add_all(rows)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _seed_claim(
    owner_id: uuid.UUID,
    claim_id: str,
    *,
    status: str = "Submitted",
    created_at: datetime | None = None,
) -> str:
    """Seed one claim and return its identifier.

    Args:
        owner_id: The user the claim belongs to.
        claim_id: The human-readable claim identifier.
        status: Claim status to start from.
        created_at: Optional filing timestamp.

    Returns:
        str: The claim identifier that was seeded.
    """
    _seed_claims(
        [
            _claim_row(
                owner_id,
                claim_id,
                status=status,
                created_at=created_at or datetime(2026, 5, 4, tzinfo=UTC),
            )
        ]
    )
    return claim_id


def _change_status(
    client: TestClient,
    headers: dict[str, str],
    claim_id: str,
    status: str,
    note: str | None = None,
) -> httpx.Response:
    """POST a status change to the lifecycle endpoint.

    Args:
        client: The test client to call through.
        headers: The caller's bearer headers.
        claim_id: The claim to move.
        status: The status to move it to.
        note: Optional note to record on the transition.

    Returns:
        TestClient.response_class: The endpoint's response.
    """
    payload: dict[str, str] = {"status": status}
    if note is not None:
        payload["note"] = note
    return client.post(f"/api/v1/claims/{claim_id}/status", json=payload, headers=headers)


def _events(client: TestClient, headers: dict[str, str], claim_id: str) -> list[dict]:
    """Read one claim's lifecycle history through the detail endpoint.

    Args:
        client: The test client to call through.
        headers: The caller's bearer headers.
        claim_id: The claim whose history is read.

    Returns:
        list[dict]: The lifecycle events, newest first.
    """
    response = client.get(f"/api/v1/claims/{claim_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["events"]


def _count_embedding_jobs(claim_id: str) -> int:
    """Count the pending embedding jobs recorded for one claim.

    Args:
        claim_id: The human-readable claim identifier to count for.

    Returns:
        int: The number of ``embedding_jobs`` rows naming that claim.
    """

    async def _count() -> int:
        async with async_session_factory() as session:
            return await session.scalar(
                select(func.count())
                .select_from(EmbeddingJob)
                .where(EmbeddingJob.claim_id == claim_id)
            )

    return asyncio.run(_count())


@pytest.mark.parametrize(("source", "target"), _ALLOWED_TRANSITIONS)
def test_every_allowed_transition_records_an_event(
    test_client: TestClient,
    as_claims_handler,
    source: ClaimStatus,
    target: ClaimStatus,
) -> None:
    """Each transition the map allows succeeds and leaves an event behind."""
    owner_id, headers = _signup(test_client, f"lifecycle_allow_{source.name}_{target.name}"[:60])
    as_claims_handler(owner_id)
    claim_id = _seed_claim(owner_id, f"CLM-{source.name[:3]}{target.name[:3]}", status=source.value)

    response = _change_status(test_client, headers, claim_id, target.value)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == target.value
    assert body["previous_status"] == source.value
    assert body["claim_id"] == claim_id
    events = _events(test_client, headers, claim_id)
    assert [(event["from_status"], event["to_status"]) for event in events] == [
        (source.value, target.value)
    ]
    assert events[0]["actor_id"] == str(owner_id)


def test_a_policyholder_may_withdraw_their_own_claim(test_client: TestClient) -> None:
    """Withdrawal needs no handler authority, because it is the claimant's decision."""
    owner_id, headers = _signup(test_client, "lifecycle_owner_withdraw")
    claim_id = _seed_claim(owner_id, "CLM-OWNWD")

    response = _change_status(test_client, headers, claim_id, "Withdrawn", note="Moved house")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "Withdrawn"
    events = _events(test_client, headers, claim_id)
    assert events[0]["from_status"] == "Submitted"
    assert events[0]["to_status"] == "Withdrawn"
    assert events[0]["note"] == "Moved house"
    assert response.json()["allowed_next_statuses"] == []


@pytest.mark.parametrize(("source", "target"), _ILLEGAL_TRANSITIONS)
def test_an_illegal_transition_is_refused_with_the_allowed_statuses_named(
    test_client: TestClient,
    as_claims_handler,
    source: ClaimStatus,
    target: ClaimStatus,
) -> None:
    """A transition outside the map is refused, and the refusal names what is allowed."""
    owner_id, headers = _signup(test_client, f"lifecycle_illegal_{source.name}_{target.name}"[:60])
    as_claims_handler(owner_id)
    claim_id = _seed_claim(
        owner_id, f"CLM-X{source.name[:3]}{target.name[:3]}", status=source.value
    )

    response = _change_status(test_client, headers, claim_id, target.value)

    assert response.status_code == 409, response.text
    message = response.json()["error"]["message"]
    assert response.json()["error"]["code"] == "CONFLICT"
    allowed = sorted(next_.value for next_ in ALLOWED_STATUS_TRANSITIONS[source])
    if allowed:
        assert all(state in message for state in allowed), message
        assert "Allowed next statuses" in message
    else:
        assert "no allowed next status" in message
    # The refused transition wrote nothing: not the status, not an event.
    assert _events(test_client, headers, claim_id) == []


def test_withdrawn_cannot_follow_an_approved_claim(test_client: TestClient) -> None:
    """A decided claim is no longer the claimant's to withdraw."""
    owner_id, headers = _signup(test_client, "lifecycle_approved_withdraw")
    claim_id = _seed_claim(owner_id, "CLM-APPWD", status="Approved")

    response = _change_status(test_client, headers, claim_id, "Withdrawn")

    assert response.status_code == 409
    assert "Approved" in response.json()["error"]["message"]
    assert _events(test_client, headers, claim_id) == []


def test_withdrawing_another_users_claim_is_not_found(test_client: TestClient) -> None:
    """A claim id belonging to someone else is reported as not found, not forbidden."""
    owner_id, owner_headers = _signup(test_client, "lifecycle_withdraw_owner")
    _, intruder_headers = _signup(test_client, "lifecycle_withdraw_intruder")
    claim_id = _seed_claim(owner_id, "CLM-FRWD")

    via_status = _change_status(test_client, intruder_headers, claim_id, "Withdrawn")
    via_withdraw = test_client.post(
        f"/api/v1/claims/{claim_id}/withdraw", json={"reason": "Not mine"}, headers=intruder_headers
    )

    assert via_status.status_code == 404
    assert via_status.json()["error"]["code"] == "NOT_FOUND"
    assert via_withdraw.status_code == 404
    assert _events(test_client, owner_headers, claim_id) == []


def test_a_second_transition_enqueues_its_own_job(
    test_client: TestClient,
    as_claims_handler,
) -> None:
    """Each transition gets its own job, so the index catches the newest status.

    The vector store copies the status into its metadata at embed time, so a claim
    that moved on without a job would keep answering search with the status it
    had when it was filed.
    """
    owner_id, headers = _signup(test_client, "lifecycle_reindex")
    as_claims_handler(owner_id)
    claim_id = _seed_claim(owner_id, "CLM-RIDX")
    assert _count_embedding_jobs(claim_id) == 0

    first = _change_status(test_client, headers, claim_id, "Under Review")
    second = _change_status(test_client, headers, claim_id, "Info Requested")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert _count_embedding_jobs(claim_id) == 2


def test_status_history_is_returned_newest_first(
    test_client: TestClient,
    as_claims_handler,
) -> None:
    """The detail endpoint lists the most recent transition first."""
    owner_id, headers = _signup(test_client, "lifecycle_history_order")
    as_claims_handler(owner_id)
    claim_id = _seed_claim(owner_id, "CLM-HORD")

    _change_status(test_client, headers, claim_id, "Under Review")
    _change_status(test_client, headers, claim_id, "Info Requested", note="Please send the invoice")
    _change_status(test_client, headers, claim_id, "Under Review")
    _change_status(test_client, headers, claim_id, "Approved")

    events = _events(test_client, headers, claim_id)

    assert [(event["from_status"], event["to_status"]) for event in events] == [
        ("Under Review", "Approved"),
        ("Info Requested", "Under Review"),
        ("Under Review", "Info Requested"),
        ("Submitted", "Under Review"),
    ]
    timestamps = [event["created_at"] for event in events]
    assert timestamps == sorted(timestamps, reverse=True)


def test_status_history_is_owner_scoped(test_client: TestClient, as_claims_handler) -> None:
    """One policyholder's transition history never reaches another's response."""
    owner_id, owner_headers = _signup(test_client, "lifecycle_hist_owner")
    other_id, other_headers = _signup(test_client, "lifecycle_hist_other")
    as_claims_handler(owner_id)
    as_claims_handler(other_id)
    _seed_claim(owner_id, "CLM-HSCO")
    _seed_claim(other_id, "CLM-HSCT")

    _change_status(test_client, owner_headers, "CLM-HSCO", "Under Review")
    _change_status(test_client, other_headers, "CLM-HSCT", "Under Review")

    assert len(_events(test_client, owner_headers, "CLM-HSCO")) == 1
    assert len(_events(test_client, other_headers, "CLM-HSCT")) == 1
    # The other policyholder's claim is not reported as forbidden; it is not
    # reported at all.
    assert test_client.get("/api/v1/claims/CLM-HSCO", headers=other_headers).status_code == 404


def test_filing_a_claim_records_the_opening_event(test_client: TestClient) -> None:
    """The confirmed claim's history starts at the moment it was filed."""
    _, headers = _signup(test_client, "lifecycle_filing")

    prepared = test_client.post("/api/v1/claims/prepare", json=_VALID_SUBMISSION, headers=headers)
    assert prepared.status_code == 200
    confirmed = test_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
        headers=headers,
    )
    assert confirmed.status_code == 200
    claim_id = confirmed.json()["claim_id"]

    events = _events(test_client, headers, claim_id)

    assert [(event["from_status"], event["to_status"]) for event in events] == [(None, "Submitted")]


def test_a_claim_outside_the_lifecycle_cannot_be_moved(
    test_client: TestClient,
    as_claims_handler,
) -> None:
    """A legacy status outside the lifecycle blocks the transition instead of guessing."""
    owner_id, headers = _signup(test_client, "lifecycle_legacy")
    as_claims_handler(owner_id)
    claim_id = _seed_claim(owner_id, "CLM-LGCY", status="Open")

    response = _change_status(test_client, headers, claim_id, "Under Review")

    assert response.status_code == 409
    assert "Open" in response.json()["error"]["message"]


def test_an_administrative_transition_is_refused_for_a_policyholder(
    test_client: TestClient,
) -> None:
    """Only a claims handler may move a claim into a decision status.

    The refusal is the documented interim state: the endpoint has no way to prove
    a caller is a handler yet, so it refuses the transition to everyone rather
    than letting any logged-in user approve a claim.
    """
    owner_id, headers = _signup(test_client, "lifecycle_no_handler")
    claim_id = _seed_claim(owner_id, "CLM-NHD")

    response = _change_status(test_client, headers, claim_id, "Under Review")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    # Nothing was written: neither the status nor the re-index job.
    assert _events(test_client, headers, claim_id) == []
    assert _count_embedding_jobs(claim_id) == 0


def test_an_unknown_status_is_rejected_before_anything_is_written(
    test_client: TestClient,
) -> None:
    """A status outside the lifecycle is a bad request, not a conflict."""
    owner_id, headers = _signup(test_client, "lifecycle_unknown_status")
    claim_id = _seed_claim(owner_id, "CLM-UNKS")

    response = test_client.post(
        f"/api/v1/claims/{claim_id}/status", json={"status": "Reopened"}, headers=headers
    )

    assert response.status_code == 422
    assert "Reopened" in response.json()["error"]["message"]
    assert _events(test_client, headers, claim_id) == []


def test_the_withdraw_endpoint_moves_the_claim_and_records_the_reason(
    test_client: TestClient,
) -> None:
    """The user-facing wrapper is restricted to the withdrawal transition."""
    owner_id, headers = _signup(test_client, "lifecycle_withdraw_wrapper")
    claim_id = _seed_claim(owner_id, "CLM-WDRW")

    response = test_client.post(
        f"/api/v1/claims/{claim_id}/withdraw",
        json={"reason": "The damage turned out to be minor."},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previous_status"] == "Submitted"
    assert body["status"] == "Withdrawn"
    assert body["note"] == "The damage turned out to be minor."
    events = _events(test_client, headers, claim_id)
    assert events[0]["to_status"] == "Withdrawn"
    assert events[0]["note"] == "The damage turned out to be minor."


def test_cancelling_marks_the_pending_submission_cancelled(test_client: TestClient) -> None:
    """A cancelled submission can never be redeemed into a claim."""
    _, headers = _signup(test_client, "lifecycle_cancel_ok")

    prepared = test_client.post("/api/v1/claims/prepare", json=_VALID_SUBMISSION, headers=headers)
    assert prepared.status_code == 200
    token = prepared.json()["confirmation_token"]

    cancelled = test_client.post(
        "/api/v1/claims/cancel", json={"confirmation_token": token}, headers=headers
    )

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["success"] is True
    assert body["message"]
    # The same token is refused afterwards, so cancelling really did close it.
    replayed = test_client.post(
        "/api/v1/claims/cancel", json={"confirmation_token": token}, headers=headers
    )
    assert replayed.status_code == 400
    confirmed = test_client.post(
        "/api/v1/claims/confirm", json={"confirmation_token": token}, headers=headers
    )
    assert confirmed.status_code == 400


def test_cancelling_rejects_a_token_the_caller_does_not_own(test_client: TestClient) -> None:
    """Another user's pending submission is indistinguishable from an unknown token."""
    _, owner_headers = _signup(test_client, "lifecycle_cancel_owner")
    _, intruder_headers = _signup(test_client, "lifecycle_cancel_intruder")

    prepared = test_client.post(
        "/api/v1/claims/prepare", json=_VALID_SUBMISSION, headers=owner_headers
    )
    assert prepared.status_code == 200

    response = test_client.post(
        "/api/v1/claims/cancel",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
        headers=intruder_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_cancelling_an_unknown_token_is_refused(test_client: TestClient) -> None:
    """A token that was never issued is refused the same way a used one is."""
    _, headers = _signup(test_client, "lifecycle_cancel_unknown")

    response = test_client.post(
        "/api/v1/claims/cancel", json={"confirmation_token": str(uuid.uuid4())}, headers=headers
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_cancelling_requires_authentication(test_client: TestClient) -> None:
    """An anonymous caller cannot cancel a submission."""
    response = test_client.post(
        "/api/v1/claims/cancel", json={"confirmation_token": str(uuid.uuid4())}
    )

    assert response.status_code == 401
