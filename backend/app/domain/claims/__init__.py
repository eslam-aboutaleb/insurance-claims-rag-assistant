"""Claims domain adapter: retrieval, ingestion, outbox, and the search_claims tool."""

from __future__ import annotations

from app.domain.claims.ingest import ingest_claim, ingest_all_claims
from app.domain.claims.outbox import ClaimJobProcessor, enqueue_claim_embedding_job
from app.domain.claims.retriever import (
    CLAIMS_RETRIEVER_SPEC,
    ClaimsRetriever,
    retrieve_claims_hybrid,
)
from app.domain.claims.tools import search_claims

__all__ = [
    "CLAIMS_RETRIEVER_SPEC",
    "ClaimJobProcessor",
    "ClaimsRetriever",
    "enqueue_claim_embedding_job",
    "ingest_claim",
    "ingest_all_claims",
    "retrieve_claims_hybrid",
    "search_claims",
]
