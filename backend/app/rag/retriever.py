"""Thin adapter over the ragkit hybrid retriever.

The generic retriever moved to :mod:`ragkit.retrieval`
(ragkit plan 04). This module keeps the historical
import path working and binds the retriever to the
OmniCare policy chunk table: the hardcoded
``policy_chunks`` binding below moves to
``app/domain/policies/`` in plan 06.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.database import async_session_factory
from ragkit.retrieval import HybridRetriever, RetrieverSpec, as_dicts

logger = logging.getLogger(__name__)

_POLICY_CHUNKS_SPEC = RetrieverSpec(
    table_name="policy_chunks",
    metadata_fields=["section", "source", "chunk_index", "sub_chunk_index"],
)


async def retrieve_hybrid(
    query: str,
    n_results: int = 5,
    distance_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval: vector search + PostgreSQL full-text search using RRF.

    Orchestrates the two-stage retrieval pipeline:
      1. Embeds the query using the configured embedding function.
      2. Delegates to the vector store's ``hybrid_search`` method, which
         executes parallel vector similarity and PostgreSQL full-text search
         CTEs and merges results via Reciprocal Rank Fusion.

    Args:
        query: The natural-language search query.
        n_results: Maximum number of candidates to fetch.
        distance_threshold: Maximum L2 distance for the vector search part.
            If None, reads from ``settings.rag_distance_threshold``.

    Returns:
        List of dicts (sorted by RRF score) with keys:
            - ``document`` (str): Chunk text.
            - ``metadata`` (dict): Section, source, and chunk index fields.
            - ``distance`` (float): The L2 distance (if found by vector search).
    """
    settings = get_settings()
    if distance_threshold is None:
        distance_threshold = settings.rag_distance_threshold

    try:
        retriever = HybridRetriever(
            spec=_POLICY_CHUNKS_SPEC,
            settings=settings,
            session_factory=async_session_factory,
        )
        results = await retriever.retrieve(
            query,
            n_results=n_results,
            distance_threshold=distance_threshold,
        )
        return as_dicts(results)
    except Exception as exc:
        logger.error("Hybrid retrieval failed: %s", exc)
        return []
