import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.submit_claim import prepare_claim_submission, submit_claim_internal
from app.api.v1.claims import confirm_claim_submission
from app.models.claims.claim_submission import ClaimSubmission as ClaimSubmissionModel
from app.schemas.models import ClaimConfirmationRequest


@pytest.mark.asyncio
async def test_prepare_claim_submission_success():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    with (
        patch("app.agent.tools.submit_claim.async_session_factory") as mock_factory,
    ):
        mock_factory.return_value.__aenter__.return_value = mock_session

        from app.agent.context import current_user_id

        current_user_id.set(uuid.uuid4())

        result = await prepare_claim_submission(
            policy_number="POL-1092",
            claim_type="Water Damage",
            amount=2500.00,
            description="Frozen pipe burst causing water damage to hardwood floors",
        )

        assert "error" not in result
        assert result.get("success") is True
        assert "confirmation_token" in result
        assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_prepare_claim_submission_invalid_negative_amount():
    from app.agent.context import current_user_id

    current_user_id.set(uuid.uuid4())
    result = await prepare_claim_submission(
        policy_number="POL-1092",
        claim_type="Water Damage",
        amount=-500.00,
        description="Negative amount",
    )
    assert result.get("success") is False
    assert "validation_errors" in result


@pytest.mark.asyncio
async def test_prepare_claim_submission_short_description():
    from app.agent.context import current_user_id

    current_user_id.set(uuid.uuid4())
    result = await prepare_claim_submission(
        policy_number="POL-1092", claim_type="Water Damage", amount=150.00, description="Too short"
    )
    assert result.get("success") is False
    assert "validation_errors" in result


@pytest.mark.asyncio
async def test_submit_claim_internal_rolls_back_on_job_failure():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock(side_effect=Exception("DB error on commit"))

    with patch("app.agent.tools.submit_claim.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__.return_value = mock_session

        result = await submit_claim_internal(
            policy_number="POL-1092",
            claim_type="Water Damage",
            amount=2500.00,
            description="Frozen pipe burst causing water damage to hardwood floors",
            user_uuid=uuid.uuid4(),
        )

    assert result.get("success") is False
    assert "error" in result
    assert mock_session.add.call_count == 2
    assert mock_session.commit.call_count == 1


@pytest.mark.asyncio
async def test_submit_claim_internal_accepts_explicit_session():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    new_claim = MagicMock()
    new_claim.id = uuid.uuid4()
    new_claim.claim_id = "CLM-TEST"
    new_claim.owner_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    new_claim.claim_type = "Water Damage"
    new_claim.description = "Pipe burst"
    new_claim.policy_number = "POL-1092"
    new_claim.status = "Submitted"
    new_claim.amount = 1500.0

    with (
        patch("app.agent.tools.submit_claim.Claim", return_value=new_claim),
        patch(
            "app.agent.tools.submit_claim.uuid.uuid4",
            return_value=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        ),
    ):
        result = await submit_claim_internal(
            policy_number="POL-1092",
            claim_type="Water Damage",
            amount=1500.0,
            description="Pipe burst",
            user_uuid=uuid.UUID("00000000-0000-0000-0000-000000000003"),
            session=mock_session,
        )

    assert result.get("success") is True
    assert result["confirmation_id"] == "CLM-00000000"
    # The tool must not commit a caller-owned session: doing so releases the
    # SELECT ... FOR UPDATE row lock that confirm_claim_submission holds on the
    # pending submission, which allowed a second concurrent confirmation to insert a
    # duplicate claim for the same token. It flushes so the caller can still commit
    # the claim, the job, and the status change together.
    mock_session.commit.assert_not_called()
    mock_session.flush.assert_awaited()


@pytest.mark.asyncio
async def test_submit_claim_internal_commits_when_it_owns_the_session():
    """With no caller session the tool owns the transaction and must commit it."""
    new_claim = MagicMock()
    new_claim.id = uuid.uuid4()
    new_claim.claim_id = "CLM-OWNED"
    new_claim.owner_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    new_claim.claim_type = "Water Damage"
    new_claim.description = "Pipe burst"
    new_claim.policy_number = "POL-1092"
    new_claim.status = "Submitted"
    new_claim.amount = 1500.0

    owned_session = AsyncMock()
    owned_session.add = MagicMock()

    with (
        patch("app.agent.tools.submit_claim.Claim", return_value=new_claim),
        patch(
            "app.agent.tools.submit_claim.uuid.uuid4",
            return_value=uuid.UUID("00000000-0000-0000-0000-000000000005"),
        ),
        patch("app.agent.tools.submit_claim.async_session_factory") as mock_factory,
    ):
        mock_factory.return_value.__aenter__.return_value = owned_session
        mock_factory.return_value.__aexit__.return_value = False
        result = await submit_claim_internal(
            policy_number="POL-1092",
            claim_type="Water Damage",
            amount=1500.0,
            description="Pipe burst",
            user_uuid=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        )

    assert result.get("success") is True
    owned_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_claim_submission_marks_completed_on_success():
    token = str(uuid.uuid4())
    user_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    future_expiry = datetime.now(UTC) + __import__("datetime").timedelta(minutes=15)

    mock_submission = MagicMock(spec=ClaimSubmissionModel)
    mock_submission.confirmation_token = token
    mock_submission.user_id = user_uuid
    mock_submission.status = "pending"
    mock_submission.expires_at = future_expiry
    mock_submission.claim_data = {
        "policy_number": "POL-1092",
        "claim_type": "Water Damage",
        "amount": "2500.00",
        "description": "Frozen pipe burst causing water damage to hardwood floors",
    }

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_submission
    mock_session.execute.return_value = mock_result
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    with (
        patch("app.api.v1.claims.async_session_factory") as mock_factory,
        patch("app.api.v1.claims.submit_claim_internal") as mock_submit,
    ):
        mock_factory.return_value = mock_session
        mock_submit.return_value = {
            "success": True,
            "confirmation_id": "CLM-1234",
            "status": "Submitted",
            "message": "Claim submitted successfully.",
        }

        payload = ClaimConfirmationRequest(confirmation_token=token)
        response = await confirm_claim_submission(payload, current_user_id=str(user_uuid))

    assert response.success is True
    assert response.claim_id == "CLM-1234"
    mock_submit.assert_called_once()
    mock_session.commit.assert_called_once()
    assert mock_submission.status == "completed"


@pytest.mark.asyncio
async def test_confirm_claim_submission_leaves_token_pending_on_failure():
    token = str(uuid.uuid4())
    user_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    future_expiry = datetime.now(UTC) + __import__("datetime").timedelta(minutes=15)

    mock_submission = MagicMock(spec=ClaimSubmissionModel)
    mock_submission.confirmation_token = token
    mock_submission.user_id = user_uuid
    mock_submission.status = "pending"
    mock_submission.expires_at = future_expiry
    mock_submission.claim_data = {
        "policy_number": "POL-1092",
        "claim_type": "Water Damage",
        "amount": "2500.00",
        "description": "Frozen pipe burst causing water damage to hardwood floors",
    }

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_submission
    mock_session.execute.return_value = mock_result
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    with (
        patch("app.api.v1.claims.async_session_factory") as mock_factory,
        patch("app.api.v1.claims.submit_claim_internal") as mock_submit,
    ):
        mock_factory.return_value = mock_session
        mock_submit.return_value = {
            "success": False,
            "error": "DB connection lost.",
        }

        payload = ClaimConfirmationRequest(confirmation_token=token)
        try:
            await confirm_claim_submission(payload, current_user_id=str(user_uuid))
        except Exception:
            pass

    mock_submit.assert_called_once()
    assert mock_submission.status == "pending"
    mock_session.commit.assert_not_called()
