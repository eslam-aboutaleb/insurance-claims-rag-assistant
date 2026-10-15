"""Tests for the owner-scoped claims read endpoints.

Every scenario drives the public API with a real bearer token. Claim rows are seeded
directly so a scenario can fix the status, claim type, and filing order it needs; the
autouse schema-reset fixture in ``tests/conftest.py`` wipes every table before each
test, so a seeded row is visible to exactly one test.

Seeding runs on its own event loop around ``engine.dispose()``, mirroring the reset
fixture. The engine is NullPool under ``ENVIRONMENT=test``, so a loop switch cannot
hand a live connection to a loop that did not create it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.claims import router as claims_router
from app.security.auth import create_access_token
from app.core.database import async_session_factory, engine
from app.models.claims.claim import Claim
from app.models.user import User

_TEST_PASSWORD = "ReadApiPass123!"  # noqa: S105 - fixture credential, not a secret

_VALID_SUBMISSION = {
    "policy_number": "POL-7781",
    "claim_type": "Water Damage",
    "amount": 2500.50,
    "description": "A frozen pipe burst flooded the basement overnight.",
}


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
    incident_date: date | None = None,
) -> Claim:
    """Build one claim row for seeding.

    Args:
        owner_id: The user the claim belongs to.
        claim_id: The human-readable claim identifier.
        status: Claim status to store.
        claim_type: Claim category to store.
        amount: Claimed amount, stored exactly as ``Numeric(12, 2)`` would keep it.
        created_at: Filing timestamp, so ordering is deterministic per test.
        incident_date: Optional incident date.

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
        incident_date=incident_date,
    )


def _seed_claims(rows: list[Claim]) -> None:
    """Persist claim rows so the read endpoints have data to scope.

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


def test_list_returns_only_the_callers_claims(test_client: TestClient) -> None:
    """Another policyholder's claim never reaches the caller's list."""
    owner_id, owner_headers = _signup(test_client, "claims_reader_a")
    other_id, _ = _signup(test_client, "claims_reader_b")
    _seed_claims(
        [
            _claim_row(owner_id, "CLM-1001", created_at=datetime(2026, 1, 5, tzinfo=UTC)),
            _claim_row(owner_id, "CLM-1002", created_at=datetime(2026, 2, 5, tzinfo=UTC)),
            _claim_row(other_id, "CLM-2001", created_at=datetime(2026, 3, 5, tzinfo=UTC)),
        ]
    )

    response = test_client.get("/api/v1/claims", headers=owner_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert {claim["claim_id"] for claim in body["claims"]} == {"CLM-1001", "CLM-1002"}


def test_list_is_empty_for_a_user_with_no_claims(test_client: TestClient) -> None:
    """A new policyholder sees an empty page, not an error."""
    _, headers = _signup(test_client, "claims_reader_new")

    response = test_client.get("/api/v1/claims", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"claims": [], "count": 0, "limit": 20, "offset": 0}


def test_list_filters_by_status(test_client: TestClient) -> None:
    """The status filter narrows the list to claims in that status."""
    owner_id, headers = _signup(test_client, "claims_reader_status")
    _seed_claims(
        [
            _claim_row(
                owner_id,
                "CLM-1101",
                status="Submitted",
                created_at=datetime(2026, 1, 5, tzinfo=UTC),
            ),
            _claim_row(
                owner_id,
                "CLM-1102",
                status="Under Review",
                created_at=datetime(2026, 2, 5, tzinfo=UTC),
            ),
        ]
    )

    response = test_client.get("/api/v1/claims", params={"status": "Under Review"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["claims"][0]["claim_id"] == "CLM-1102"
    assert body["claims"][0]["status"] == "Under Review"


def test_list_filters_by_claim_type(test_client: TestClient) -> None:
    """The claim_type filter narrows the list to that category."""
    owner_id, headers = _signup(test_client, "claims_reader_type")
    _seed_claims(
        [
            _claim_row(
                owner_id,
                "CLM-1201",
                claim_type="Water Damage",
                created_at=datetime(2026, 1, 5, tzinfo=UTC),
            ),
            _claim_row(
                owner_id,
                "CLM-1202",
                claim_type="Personal Property",
                created_at=datetime(2026, 2, 5, tzinfo=UTC),
            ),
        ]
    )

    response = test_client.get(
        "/api/v1/claims", params={"claim_type": "Personal Property"}, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["claims"][0]["claim_id"] == "CLM-1202"
    assert body["claims"][0]["claim_type"] == "Personal Property"


def test_list_orders_newest_first_by_default(test_client: TestClient) -> None:
    """Without an explicit sort the newest claim comes first."""
    owner_id, headers = _signup(test_client, "claims_reader_order")
    _seed_claims(
        [
            _claim_row(owner_id, "CLM-1301", created_at=datetime(2026, 1, 5, tzinfo=UTC)),
            _claim_row(owner_id, "CLM-1303", created_at=datetime(2026, 3, 5, tzinfo=UTC)),
            _claim_row(owner_id, "CLM-1302", created_at=datetime(2026, 2, 5, tzinfo=UTC)),
        ]
    )

    response = test_client.get("/api/v1/claims", headers=headers)

    assert response.status_code == 200
    assert [claim["claim_id"] for claim in response.json()["claims"]] == [
        "CLM-1303",
        "CLM-1302",
        "CLM-1301",
    ]


def test_list_honours_pagination_limits(test_client: TestClient) -> None:
    """``limit`` and ``offset`` page through the caller's claims."""
    owner_id, headers = _signup(test_client, "claims_reader_page")
    _seed_claims(
        [
            _claim_row(
                owner_id, f"CLM-14{index:02d}", created_at=datetime(2026, 1, index, tzinfo=UTC)
            )
            for index in range(1, 4)
        ]
    )

    first = test_client.get("/api/v1/claims", params={"limit": 2, "offset": 0}, headers=headers)
    second = test_client.get("/api/v1/claims", params={"limit": 2, "offset": 2}, headers=headers)
    over_max = test_client.get("/api/v1/claims", params={"limit": 51}, headers=headers)

    assert first.status_code == 200
    assert first.json()["count"] == 2
    assert first.json()["offset"] == 0
    assert second.status_code == 200
    assert second.json()["count"] == 1
    assert second.json()["offset"] == 2
    # The page size cap is enforced, not silently clamped.
    assert over_max.status_code == 422


def test_list_requires_authentication(test_client: TestClient) -> None:
    """An anonymous caller cannot read the claim list."""
    response = test_client.get("/api/v1/claims")

    assert response.status_code == 401


def test_detail_returns_the_callers_claim(test_client: TestClient) -> None:
    """The detail response carries the claim, its description, and a citation."""
    owner_id, headers = _signup(test_client, "claims_reader_detail")
    _seed_claims(
        [
            _claim_row(
                owner_id,
                "CLM-1501",
                status="Under Review",
                amount="3500.75",
                created_at=datetime(2026, 2, 14, tzinfo=UTC),
                incident_date=date(2026, 2, 1),
            )
        ]
    )

    response = test_client.get("/api/v1/claims/CLM-1501", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["claim_id"] == "CLM-1501"
    assert body["policy_number"] == "POL-1501"
    assert body["claim_type"] == "Water Damage"
    assert body["status"] == "Under Review"
    assert body["amount"] == "3500.75"
    assert body["incident_date"] == "2026-02-01"
    assert body["created_at"].startswith("2026-02-14")
    assert body["description"] == "Factual description for CLM-1501."
    assert body["citation"] == ("Claim CLM-1501 in the OmniCare claims system (policy POL-1501).")
    # Lifecycle events are not recorded yet; the placeholder keeps the shape stable.
    assert body["events"] == []


def test_detail_matches_the_citation_the_agent_returns(test_client: TestClient) -> None:
    """The citation string is byte-identical to the one the agent's tool returns."""
    owner_id, headers = _signup(test_client, "claims_reader_citation")
    _seed_claims([_claim_row(owner_id, "CLM-1601", created_at=datetime(2026, 2, 14, tzinfo=UTC))])

    tool_result = asyncio.run(_claim_status_as(owner_id, "CLM-1601"))
    assert tool_result["found"] is True

    response = test_client.get("/api/v1/claims/CLM-1601", headers=headers)

    assert response.status_code == 200
    assert response.json()["citation"] == tool_result["citation"]


async def _claim_status_as(owner_id: uuid.UUID, claim_id: str) -> dict:
    """Run the agent's claim status tool scoped to ``owner_id``.

    Args:
        owner_id: The user the lookup is scoped to.
        claim_id: The claim identifier to look up.

    Returns:
        dict: The tool's result payload, including its ``citation``.
    """
    from app.agent.context import current_user_id
    from app.agent.tools.claim_status import get_claim_status

    token = current_user_id.set(owner_id)
    try:
        return await get_claim_status(claim_id)
    finally:
        current_user_id.reset(token)


def test_detail_for_another_users_claim_is_404(test_client: TestClient) -> None:
    """A claim id belonging to someone else is reported as not found, not forbidden."""
    owner_id, owner_headers = _signup(test_client, "claims_reader_owner")
    other_id, other_headers = _signup(test_client, "claims_reader_intruder")
    _seed_claims([_claim_row(owner_id, "CLM-1701", created_at=datetime(2026, 2, 14, tzinfo=UTC))])
    assert other_id != owner_id

    response = test_client.get("/api/v1/claims/CLM-1701", headers=other_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_detail_is_case_insensitive_on_the_claim_id(test_client: TestClient) -> None:
    """The agent's lookup upper-cases the id, so the REST path does too."""
    owner_id, headers = _signup(test_client, "claims_reader_case")
    _seed_claims([_claim_row(owner_id, "CLM-1801", created_at=datetime(2026, 2, 14, tzinfo=UTC))])

    response = test_client.get("/api/v1/claims/clm-1801", headers=headers)

    assert response.status_code == 200
    assert response.json()["claim_id"] == "CLM-1801"


def test_detail_requires_authentication(test_client: TestClient) -> None:
    """An anonymous caller cannot read a single claim."""
    response = test_client.get("/api/v1/claims/CLM-9999")

    assert response.status_code == 401


def test_prepare_and_confirm_records_the_incident_date(test_client: TestClient) -> None:
    """An incident date supplied at prepare time survives the confirm handoff."""
    _, headers = _signup(test_client, "claims_reader_incident")

    prepared = test_client.post(
        "/api/v1/claims/prepare",
        params={"incident_date": "2026-03-04"},
        json=_VALID_SUBMISSION,
        headers=headers,
    )
    assert prepared.status_code == 200

    confirmed = test_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
        headers=headers,
    )
    assert confirmed.status_code == 200

    detail = test_client.get(f"/api/v1/claims/{confirmed.json()['claim_id']}", headers=headers)

    assert detail.status_code == 200
    assert detail.json()["incident_date"] == "2026-03-04"


def test_a_claim_confirmed_without_an_incident_date_keeps_it_null(
    test_client: TestClient,
) -> None:
    """Omitting the incident date leaves the column null rather than guessing."""
    _, headers = _signup(test_client, "claims_reader_no_incident")

    prepared = test_client.post("/api/v1/claims/prepare", json=_VALID_SUBMISSION, headers=headers)
    assert prepared.status_code == 200

    confirmed = test_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
        headers=headers,
    )
    assert confirmed.status_code == 200

    detail = test_client.get(f"/api/v1/claims/{confirmed.json()['claim_id']}", headers=headers)

    assert detail.status_code == 200
    assert detail.json()["incident_date"] is None


def test_read_endpoints_work_with_only_the_claims_router_mounted() -> None:
    """The endpoints are self-contained, not dependent on the v1 router registration.

    Mounting the claims router on a bare ``FastAPI`` app proves the endpoints need no
    middleware, no sibling router, and no startup hook beyond the dependencies they
    declare.
    """
    owner_id = uuid.uuid4()

    async def _seed() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                session.add(
                    User(
                        id=owner_id,
                        username=f"standalone_{owner_id.hex[:8]}",
                        password_hash="unused-hash",  # noqa: S106 - fixture value, not a credential
                    )
                )
                # The claim rows are flushed only after the owner exists: the two
                # models share no relationship, so the unit of work would otherwise
                # emit the claim insert first and trip the foreign key.
                await session.flush()
                session.add(
                    _claim_row(owner_id, "CLM-1901", created_at=datetime(2026, 2, 14, tzinfo=UTC))
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())

    standalone_app = FastAPI()
    standalone_app.include_router(claims_router, prefix="/api/v1/claims")
    headers = {"Authorization": f"Bearer {create_access_token(owner_id)}"}

    with TestClient(standalone_app) as client:
        listed = client.get("/api/v1/claims", headers=headers)
        detail = client.get("/api/v1/claims/CLM-1901", headers=headers)
        anonymous = client.get("/api/v1/claims")

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert detail.status_code == 200
    assert detail.json()["claim_id"] == "CLM-1901"
    assert anonymous.status_code == 401
