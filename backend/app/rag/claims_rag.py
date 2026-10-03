"""
Deprecated shim for claims RAG operations.

The OmniCare claims binding moved to
:mod:`app.domain.claims` (ragkit plan 06):
retrieval to :mod:`app.domain.claims.retriever`,
ingestion to :mod:`app.domain.claims.ingest`.
This module keeps the historical import path —
and the historical patch points
(``get_vector_store``, ``EmbeddingFactory``,
``async_session_factory``, ``ingest_claim``) —
working until plan 07 removes the shims.

.. deprecated::
    Use :mod:`app.domain.claims` instead.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session_factory
from app.domain.claims.retriever import CLAIMS_RETRIEVER_SPEC
from app.models.claim import Claim
from app.rag.embedding import EmbeddingFactory
from app.rag.embedding_dimensions import get_embedding_dimension
from app.rag.pgvector_store import _validate_embedding
from app.rag.vector_store import get_vector_store

logger = logging.getLogger(__name__)

__all__ = [
    "CLAIMS_RETRIEVER_SPEC",
    "retrieve_claims_hybrid",
    "ingest_claim",
    "ingest_all_claims",
]

settings = get_settings()


async def retrieve_claims_hybrid(
    query: str,
    user_id: uuid.UUID,
    n_results: int = 5,
    distance_threshold: float | None = None,
) -> list[dict]:
    """
    Hybrid search over claims (vector + PostgreSQL full-text).

    .. deprecated::
        Use :func:`app.domain.claims.retriever.retrieve_claims_hybrid`
        instead. This shim remains until plan 07 removes it.

    Combines vector similarity search with PostgreSQL
    full-text search (tsvector) using Reciprocal Rank
    Fusion (RRF) to merge results from both methods.

    Security:
        Always applies ``owner_id`` filtering to ensure
        users can only retrieve their own claims.

    Args:
        query: Natural language query string.
        user_id: UUID of the authenticated user (for ACL filtering).
        n_results: Maximum number of results to return.
        distance_threshold: Maximum L2 distance for vector search.
            Defaults to ``settings.rag_distance_threshold``.

    Returns:
        List of result dicts with keys:
            - ``id`` (str): The claim UUID
            - ``document`` (str): The retrieved claim text
            - ``metadata`` (dict): Structured claim metadata
            - ``distance`` (float): L2 distance from vector search
            - ``keyword_score`` (float): PostgreSQL full-text rank
            - ``_rrf_score`` (float): Combined RRF score

        Returns empty list on error (errors are logged, not raised).
    """
    if distance_threshold is None:
        distance_threshold = settings.rag_distance_threshold

    try:
        store = get_vector_store(
            table_name=CLAIMS_RETRIEVER_SPEC.table_name,
            id_field=CLAIMS_RETRIEVER_SPEC.id_field,
        )
        embed_fn = EmbeddingFactory.get_embedding_function()
        query_embedding = await embed_fn([query])
        query_embedding = query_embedding[0]
        return await store.hybrid_search(
            query=query,
            embedding=query_embedding,
            n_results=n_results,
            threshold=distance_threshold,
            text_field=CLAIMS_RETRIEVER_SPEC.text_field,
            metadata_fields=CLAIMS_RETRIEVER_SPEC.metadata_fields,
            owner_id=str(user_id),
        )
    except Exception as exc:
        logger.error("Hybrid claims search failed: %s", exc)
        return []


async def ingest_claim(  # noqa: PLR0913, PLR0917
    id: uuid.UUID,
    claim_id: str,
    owner_id: uuid.UUID,
    claim_type: str,
    description: str,
    policy_number: str,
    status: str,
    amount: float,
) -> None:
    """
    Ingest a single claim into the vector store.

    .. deprecated::
        Use :func:`app.domain.claims.ingest.ingest_claim`
        instead. This shim remains until plan 07 removes it.

    Args:
        id: Internal UUID of the claim row.
        claim_id: Human-readable claim identifier.
        owner_id: UUID of the user who owns the claim.
        claim_type: Category of the claim.
        description: Factual description of the incident.
        policy_number: The policyholder's policy number.
        status: Current processing status.
        amount: Claimed amount in US dollars.
    """
    embed_fn = EmbeddingFactory.get_embedding_function()
    text_content = f"Claim {claim_id}: {claim_type} - {description}"
    embedding = await embed_fn([text_content])
    embedding = embedding[0]
    _validate_embedding(embedding, expected_dim=get_embedding_dimension(), label="claim embedding")

    store = get_vector_store(
        table_name=CLAIMS_RETRIEVER_SPEC.table_name,
        id_field=CLAIMS_RETRIEVER_SPEC.id_field,
    )
    await store.upsert(
        documents=[
            {
                "id": str(id),
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
    """
    Ingest all claims into the vector store.

    .. deprecated::
        Use :func:`app.domain.claims.ingest.ingest_all_claims`
        instead. This shim remains until plan 07 removes it.

    Reads every claim row and calls
    ``ingest_claim`` for each one. Used to
    (re)build the claims index from the database.
    """
    async with async_session_factory() as session:
        result = await session.execute(select(Claim))
        claims = result.scalars().all()

        for claim in claims:
            await ingest_claim(
                id=claim.id,
                claim_id=claim.claim_id,
                owner_id=claim.owner_id,
                claim_type=claim.claim_type,
                description=claim.description,
                policy_number=claim.policy_number,
                status=claim.status,
                amount=float(claim.amount),
            )
