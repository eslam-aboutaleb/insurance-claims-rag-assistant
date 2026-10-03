"""Claims embedding-job outbox adapter over ragkit (ragkit plan 06).

The claim embedding job is the OmniCare binding of ragkit's
generic outbox: :func:`enqueue_claim_embedding_job` writes a
pending job row through :func:`ragkit.jobs.enqueue_job` — in
the caller's transaction when a session is supplied, so the
claim row and its job row commit together — and
:class:`ClaimJobProcessor` is the ragkit ``JobProcessor``
that embeds a claim and upserts its entry into the claims
vector store (the ``claims`` binding lives in
:mod:`app.domain.claims.retriever`).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.claims.retriever import CLAIMS_RETRIEVER_SPEC
from app.rag.embedding_jobs import SqlAlchemyJobStore
from ragkit.jobs import JobPayload, enqueue_job

logger = logging.getLogger(__name__)


class ClaimJobProcessor:
    """ragkit ``JobProcessor`` that embeds a claim and upserts its index entry."""

    async def process(self, job: JobPayload) -> None:
        """Embed the claim text and upsert it into the claims vector store."""
        from sqlalchemy import select  # noqa: PLC0415

        from app.config import get_settings  # noqa: PLC0415
        from app.database import async_session_factory  # noqa: PLC0415
        from app.models.claim import Claim  # noqa: PLC0415
        from app.rag.embedding import EmbeddingFactory  # noqa: PLC0415
        from app.rag.vector_store import get_vector_store  # noqa: PLC0415
        from ragkit.embeddings import get_embedding_dimension  # noqa: PLC0415
        from ragkit.validation import validate_embedding  # noqa: PLC0415

        payload = job.payload
        claim_id = payload["claim_id"]

        async with async_session_factory() as session:
            claim_amount = await session.scalar(
                select(Claim.amount).where(Claim.claim_id == claim_id)
            )
        claim_amount = claim_amount if claim_amount is not None else 0.0

        embed_fn = EmbeddingFactory.get_embedding_function()
        text_content = f"Claim {claim_id}: {payload['claim_type']} - {payload['description']}"
        embeddings = await embed_fn([text_content])
        embedding = embeddings[0]
        validate_embedding(
            embedding,
            expected_dim=get_embedding_dimension(get_settings().embedding_model),
            label="claim embedding",
        )

        store = get_vector_store(
            table_name=CLAIMS_RETRIEVER_SPEC.table_name,
            id_field=CLAIMS_RETRIEVER_SPEC.id_field,
        )
        await store.upsert(
            documents=[
                {
                    "id": payload["claim_uuid"],
                    "text": text_content,
                    "embedding": embedding,
                    "metadata": {
                        "claim_id": claim_id,
                        "policy_number": payload["policy_number"],
                        "claim_type": payload["claim_type"],
                        "status": payload["claim_status"],
                        "amount": float(claim_amount),
                        "description": payload["description"],
                        "owner_id": payload["owner_id"],
                    },
                }
            ]
        )


async def enqueue_claim_embedding_job(  # noqa: PLR0913, PLR0917
    claim_uuid: uuid.UUID,
    claim_id: str,
    owner_id: uuid.UUID,
    claim_type: str,
    description: str,
    policy_number: str,
    claim_status: str,
    session: AsyncSession | None = None,
) -> None:
    """Add an embedding job for a newly submitted claim.

    Delegates to ragkit's :func:`ragkit.jobs.enqueue_job`
    with the caller's session: when ``session`` is supplied,
    the job row is only added and flushed, so the claim and
    its embedding job are persisted atomically by the
    caller's commit. When ``session`` is None, ragkit opens
    and commits its own transaction.

    Args:
        claim_uuid: Internal UUID of the claim row.
        claim_id: Human-readable claim identifier.
        owner_id: UUID of the user who owns the claim.
        claim_type: Category of the claim.
        description: Factual description of the incident.
        policy_number: The policyholder's policy number.
        claim_status: Current processing status.
        session: Optional existing database session. When
            provided, the job row joins the caller's
            transaction and the caller commits.
    """
    store = SqlAlchemyJobStore()
    payload = {
        "claim_uuid": str(claim_uuid),
        "claim_id": claim_id,
        "owner_id": str(owner_id),
        "claim_type": claim_type,
        "description": description,
        "policy_number": policy_number,
        "claim_status": claim_status,
    }
    try:
        await enqueue_job(store, payload, session=session)
        logger.info("Enqueued embedding job for claim '%s'.", claim_id)
    except Exception as exc:
        logger.exception("Failed to enqueue embedding job for claim '%s': %s", claim_id, exc)


__all__ = ["ClaimJobProcessor", "enqueue_claim_embedding_job"]
