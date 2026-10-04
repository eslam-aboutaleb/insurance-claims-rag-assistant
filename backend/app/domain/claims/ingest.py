"""Claims ingestion adapter over ragkit (ragkit plan 06).

Claim ingestion writes directly into the ``claims``
vector store (the binding lives in
:mod:`app.domain.claims.retriever`): the claim text is
embedded, the embedding is validated against the
configured dimension, and the claim row is upserted
with its metadata. This module is the permanent home
for the binding; the historical ``app.rag.claims_rag``
module was removed in plan 07.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.domain.claims.retriever import CLAIMS_RETRIEVER_SPEC
from app.models.claim import Claim
from app.domain.embeddings import EmbeddingFactory, get_vector_store
from ragkit.embeddings import get_embedding_dimension
from ragkit.validation import validate_embedding

logger = logging.getLogger(__name__)


async def ingest_claim(  # noqa: PLR0913, PLR0917
    claim_uuid: uuid.UUID,
    claim_id: str,
    owner_id: uuid.UUID,
    claim_type: str,
    description: str,
    policy_number: str,
    status: str,
    amount: float,
) -> None:
    """Ingest a single claim into the vector store.

    Args:
        claim_uuid: Internal UUID of the claim row.
        claim_id: Human-readable claim identifier.
        owner_id: UUID of the user who owns the claim.
        claim_type: Category of the claim.
        description: Factual description of the incident.
        policy_number: The policyholder's policy number.
        status: Current processing status.
        amount: Claimed amount in US dollars.
    """
    from app.config import get_settings  # noqa: PLC0415

    embed_fn = EmbeddingFactory.get_embedding_function()
    text_content = f"Claim {claim_id}: {claim_type} - {description}"
    embedding = await embed_fn([text_content])
    embedding = embedding[0]
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
                "id": str(claim_uuid),
                "text": text_content,
                "embedding": embedding,
                "metadata": {
                    "claim_id": claim_id,
                    "policy_number": policy_number,
                    "claim_type": claim_type,
                    "status": status,
                    "amount": amount,
                    "description": description,
                    "owner_id": str(owner_id),
                },
            }
        ]
    )


async def ingest_all_claims() -> None:
    """Ingest all claims into the vector store.

    Reads every claim row and calls
    :func:`ingest_claim` for each one. Used to
    (re)build the claims index from the database.
    """
    async with async_session_factory() as session:
        result = await session.execute(select(Claim))
        claims = result.scalars().all()

        for claim in claims:
            await ingest_claim(
                claim_uuid=claim.id,
                claim_id=claim.claim_id,
                owner_id=claim.owner_id,
                claim_type=claim.claim_type,
                description=claim.description,
                policy_number=claim.policy_number,
                status=claim.status,
                amount=float(claim.amount),
            )


__all__ = ["ingest_claim", "ingest_all_claims"]
