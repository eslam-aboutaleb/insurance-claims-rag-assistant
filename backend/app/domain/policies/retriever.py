"""Policy retrieval adapter over ragit.

The OmniCare policy chunk binding — the ``policy_chunks``
table, its ``text`` column, and the metadata columns returned
in chunk metadata — lives here as :data:`POLICY_RETRIEVER_SPEC`.
The generic hybrid retrieval machinery is ragit's
:class:`ragit.retrieval.HybridRetriever`.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.database import async_session_factory
from ragit.retrieval import HybridRetriever, RetrieverSpec, as_dicts

logger = logging.getLogger(__name__)

POLICY_RETRIEVER_SPEC = RetrieverSpec(
    table_name="policy_chunks",
    id_field="id",
    text_field="text",
    metadata_fields=["section", "source", "chunk_index", "sub_chunk_index"],
)
"""Binding of the hybrid retriever to the OmniCare policy chunk table."""


class PolicyRetriever:
    """Hybrid retrieval over the OmniCare policy chunk table.

    Wraps ragit's :class:`ragit.retrieval.HybridRetriever`
    with the policy chunk binding. Any retrieval failure is logged and reported as an empty
    result set rather than raised.
    """

    def __init__(self, settings: Any = None, session_factory: Any = None) -> None:
        """Initialize the policy retriever.

        Args:
            settings: Application settings. Defaults to
                ``get_settings()``.
            session_factory: Session provider the store opens
                its transactions with. Defaults to the
                application's ``async_session_factory``.
        """
        self._settings = settings or get_settings()
        self._session_factory = session_factory or async_session_factory
        self._retriever = HybridRetriever(
            spec=POLICY_RETRIEVER_SPEC,
            settings=self._settings,
            session_factory=self._session_factory,
        )

    async def retrieve_hybrid(  # noqa: PLR0913
        self,
        query: str,
        n_results: int = 5,
        distance_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Run hybrid (vector + PostgreSQL full-text) search over policy chunks.

        Args:
            query: Natural language query.
            n_results: Maximum number of results to return.
            distance_threshold: Maximum L2 distance for the
                vector search part. Defaults to
                ``settings.rag_distance_threshold``.

        Returns:
            List of result dicts with ``document``,
            ``metadata``, ``distance``, and ``_rrf_score`` keys.
        """
        if distance_threshold is None:
            distance_threshold = self._settings.rag_distance_threshold
        results = await self._retriever.retrieve(
            query,
            n_results=n_results,
            distance_threshold=distance_threshold,
        )
        return as_dicts(results)


async def retrieve_hybrid(  # noqa: PLR0913
    query: str,
    n_results: int = 5,
    distance_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Run hybrid search over the OmniCare policy chunk table.

    Module-level entry point. Any retrieval failure is logged and reported as an empty
    result set rather than raised.

    Args:
        query: Natural language query.
        n_results: Maximum number of results to return.
        distance_threshold: Maximum L2 distance for the
            vector search part. Defaults to
            ``settings.rag_distance_threshold``.

    Returns:
        List of result dicts, or an empty list on failure.
    """
    try:
        return await PolicyRetriever().retrieve_hybrid(
            query,
            n_results=n_results,
            distance_threshold=distance_threshold,
        )
    except Exception as exc:
        logger.error("Hybrid retrieval failed: %s", exc)
        return []
