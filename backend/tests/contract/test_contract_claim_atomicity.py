"""
Regression tests for the single-transaction guarantee of the claim confirmation flow.

The defect this pins down: ``submit_claim._submit`` called ``session.commit()`` even
when the caller passed its own session. ``confirm_claim_submission`` had already taken
``SELECT ... FOR UPDATE`` on the submission row, so that commit released the row lock.
A second concurrent confirmation could then re-read the same row as ``pending`` and
insert a second claim for a single confirmation token.

The invariant that matters: one confirmation token yields exactly one claim, and
``_submit`` never commits a transaction it does not own.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.agent.tools.submit_claim import submit_claim_internal
from tests.contract.conftest import SeededUser

pytestmark = pytest.mark.contract

_VALID_CLAIM = {
    "policy_number": "POL-1092",
    "claim_type": "Water Damage",
    "amount": 5000,
    "description": "A pipe burst flooded the kitchen yesterday.",
}


class _RecordingSession:
    """Minimal session double that records flush and commit calls separately."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


def test_submit_claim_never_commits_a_caller_supplied_session() -> None:
    """``submit_claim_internal`` must flush, not commit, on a caller-owned session.

    Committing inside the tool releases the ``FOR UPDATE`` row lock the confirmation
    endpoint relies on, which is what allowed two claims for a single token.
    """
    session = _RecordingSession()

    result = asyncio.run(
        submit_claim_internal(
            policy_number=_VALID_CLAIM["policy_number"],
            claim_type=_VALID_CLAIM["claim_type"],
            amount=_VALID_CLAIM["amount"],
            description=_VALID_CLAIM["description"],
            user_uuid=uuid.uuid4(),
            session=session,  # type: ignore[arg-type]
        )
    )

    assert result["success"] is True, result
    assert session.commits == 0, "tool committed a caller-owned session"
    assert session.flushes >= 1, "tool must flush so the caller can commit later"
    assert len(session.added) == 2, "expected one claim and one embedding job"


def test_prepare_and_confirm_produce_exactly_one_claim(
    test_client: TestClient,
    user_a: SeededUser,
) -> None:
    """One token produces one claim and one embedding job, both committed together."""
    from app.core.database import async_session_factory
    from app.models.claims.claim import Claim
    from app.models.claims.embedding_job import EmbeddingJob
    from sqlalchemy import func, select

    prepared = test_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    assert prepared.status_code == 200, prepared.text
    token = prepared.json()["confirmation_token"]

    confirmed = test_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": token},
        headers=user_a.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    claim_id = confirmed.json()["claim_id"]

    async def _counts() -> tuple[int, int]:
        async with async_session_factory() as session:
            claims = await session.scalar(
                select(func.count()).select_from(Claim).where(Claim.claim_id == claim_id)
            )
            jobs = await session.scalar(
                select(func.count())
                .select_from(EmbeddingJob)
                .where(EmbeddingJob.claim_id == claim_id)
            )
            return claims, jobs

    claims, jobs = asyncio.run(_counts())
    assert claims == 1, "confirmation must create exactly one claim"
    assert jobs == 1, "confirmation must enqueue exactly one embedding job"


def test_replayed_confirmation_creates_no_second_claim(
    test_client: TestClient,
    user_a: SeededUser,
) -> None:
    """Replaying a consumed token is rejected and writes nothing further."""
    from app.core.database import async_session_factory
    from app.models.claims.claim import Claim
    from sqlalchemy import func, select

    prepared = test_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    token = prepared.json()["confirmation_token"]
    payload = {"confirmation_token": token}

    first = test_client.post("/api/v1/claims/confirm", json=payload, headers=user_a.headers)
    assert first.status_code == 200

    for _ in range(3):
        replay = test_client.post("/api/v1/claims/confirm", json=payload, headers=user_a.headers)
        assert replay.status_code == 400

    async def _total() -> int:
        async with async_session_factory() as session:
            return await session.scalar(select(func.count()).select_from(Claim))

    assert asyncio.run(_total()) == 1
