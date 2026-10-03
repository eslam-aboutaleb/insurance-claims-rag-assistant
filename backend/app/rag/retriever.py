"""
Deprecated shim for policy hybrid retrieval.

The OmniCare policy chunk binding moved to
:mod:`app.domain.policies.retriever` (ragkit
plan 06). This module keeps the historical
import path — and the historical patch points
(``HybridRetriever``) — working until plan 07
removes the shims.

.. deprecated::
    Use :mod:`app.domain.policies.retriever` instead.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.database import async_session_factory
from app.domain.policies.retriever import (
    POLICY_RETRIEVER_SPEC,
    PolicyRetriever,
)
from ragkit.retrieval import HybridRetriever, as_dicts

logger = logging.getLogger(__name__)

__all__ = [
    "POLICY_RETRIEVER_SPEC",
    "PolicyRetriever",
    "HybridRetriever",
    "as_dicts",
    "retrieve_hybrid",
]


async def retrieve_hybrid(  # noqa: PLR0913
    query: str,
    n_results: int = 5,
    distance_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """
    Hybrid search over policy documents (vector + PostgreSQL full-text).

    .. deprecated::
        Use :func:`app.domain.policies.retriever.retrieve_hybrid`
        instead. This shim remains until plan 07 removes it.

    Combines vector similarity search with PostgreSQL
    full-text search (tsvector) using Reciprocal Rank
    Fusion (RRF) to merge results from both methods.

    Args:
        query: Natural language query string.
        n_results: Maximum number of results to return.
        distance_threshold: Maximum L2 distance for vector search.
            Defaults to ``settings.rag_distance_threshold``.

    Returns:
        List of result dicts with keys:
            - ``document`` (str): The retrieved text
            - ``metadata`` (dict): Structured metadata (section, source, etc.)
            - ``distance`` (float): L2 distance from vector search
            - ``keyword_score`` (float): PostgreSQL full-text rank
            - ``_rrf_score`` (float): Combined RRF score

        Returns empty list on error (errors are logged, not raised).
    """
    settings = get_settings()
    if distance_threshold is None:
        distance_threshold = settings.rag_distance_threshold

    try:
        retriever = HybridRetriever(
            spec=POLICY_RETRIEVER_SPEC,
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
