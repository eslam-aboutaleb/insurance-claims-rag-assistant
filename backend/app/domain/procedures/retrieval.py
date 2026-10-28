"""Procedure playbook retrieval adapter over ragit.

The OmniCare procedure chunk binding — the ``procedure_chunks`` table, its
``text`` column, and the metadata columns returned in chunk metadata — lives
here as :data:`PROCEDURE_RETRIEVER_SPEC`. The generic hybrid retrieval
machinery is ragit's :class:`ragit.retrieval.HybridRetriever`.

The procedures index is a *second* index alongside the policy one, not an
extension of it: the pipeline, the chunk table, and the version table are all
separate, so re-ingesting a playbook can never disturb the policy index and a
policy re-ingest can never retire a procedure version.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.domain.rag_settings import RagitSettingsAdapter
from app.core.database import async_session_factory
from ragit.retrieval import HybridRetriever, RetrieverSpec, as_dicts

logger = logging.getLogger(__name__)

PROCEDURE_RETRIEVER_SPEC = RetrieverSpec(
    table_name="procedure_chunks",
    id_field="id",
    text_field="text",
    metadata_fields=["section", "source", "chunk_index", "sub_chunk_index"],
)
"""Binding of the hybrid retriever to the OmniCare procedure chunk table."""


class ProcedureRetriever:
    """Hybrid retrieval over the OmniCare procedure chunk table.

    Wraps ragit's :class:`ragit.retrieval.HybridRetriever` with the procedure
    chunk binding. Any retrieval failure is logged and reported as an empty
    result set rather than raised.

    Args:
        settings: Application settings. Defaults to ``get_settings()``.
        session_factory: Session provider the store opens its transactions
            with. Defaults to the application's ``async_session_factory``.
    """

    def __init__(self, settings: Any = None, session_factory: Any = None) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or async_session_factory
        self._retriever = HybridRetriever(
            spec=PROCEDURE_RETRIEVER_SPEC,
            settings=RagitSettingsAdapter(self._settings),
            session_factory=self._session_factory,
        )

    async def retrieve_hybrid(  # noqa: PLR0913
        self,
        query: str,
        n_results: int = 5,
        distance_threshold: float | None = None,
        source_file: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run hybrid (vector + PostgreSQL full-text) search over procedure chunks.

        Args:
            query: Natural language query, or the playbook's subject seed.
            n_results: Maximum number of results to return.
            distance_threshold: Maximum L2 distance for the vector search
                part. Defaults to ``settings.rag_distance_threshold``.
            source_file: Restrict the search to one playbook file. The
                filter is pushed into both search stages, so chunks from a
                different playbook are never returned.

        Returns:
            List of result dicts with ``document``, ``metadata``,
            ``distance``, and ``_rrf_score`` keys.
        """
        if distance_threshold is None:
            distance_threshold = self._settings.rag_distance_threshold
        results = await self._retriever.retrieve(
            query,
            n_results=n_results,
            distance_threshold=distance_threshold,
            **({"source": source_file} if source_file else {}),
        )
        return as_dicts(results)


async def retrieve_procedures(  # noqa: PLR0913
    query: str,
    n_results: int = 5,
    distance_threshold: float | None = None,
    source_file: str | None = None,
) -> list[dict[str, Any]]:
    """Run hybrid search over the OmniCare procedure chunk table.

    Module-level entry point. Any retrieval failure is logged and reported as
    an empty result set rather than raised.

    Args:
        query: Natural language query, or the playbook's subject seed.
        n_results: Maximum number of results to return.
        distance_threshold: Maximum L2 distance for the vector search part.
            Defaults to ``settings.rag_distance_threshold``.
        source_file: Restrict the search to one playbook file.

    Returns:
        List of result dicts, or an empty list on failure.
    """
    try:
        return await ProcedureRetriever().retrieve_hybrid(
            query,
            n_results=n_results,
            distance_threshold=distance_threshold,
            source_file=source_file,
        )
    except Exception as exc:
        logger.error("Procedure retrieval failed: %s", exc)
        return []
