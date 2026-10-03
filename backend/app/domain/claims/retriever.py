"""Claims retrieval adapter over ragkit (ragkit plan 06).

The OmniCare claims binding — the ``claims`` table, its
``description`` column, and the metadata columns returned
in claim metadata — lives here as
:data:`CLAIMS_RETRIEVER_SPEC`. The generic hybrid
retrieval machinery is ragkit's
:class:`ragkit.retrieval.HybridRetriever`.

The horizontal privilege-escalation guard lives here too:
:class:`ClaimsRetriever` always passes
``owner_id=str(user_id)`` as an equality filter, so a
caller can never see another user's claims. The filter is
applied inside the adapter, not by callers.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.config import get_settings
from app.database import async_session_factory
from ragkit.retrieval import HybridRetriever, RetrieverSpec, as_dicts

logger = logging.getLogger(__name__)

CLAIMS_RETRIEVER_SPEC = RetrieverSpec(
    table_name="claims",
    id_field="id",
    text_field="description",
    metadata_fields=[
        "claim_id",
        "policy_number",
        "claim_type",
        "status",
        "amount",
        "owner_id",
    ],
)
"""Binding of the hybrid retriever to the OmniCare claims table."""


class ClaimsRetriever:
    """Hybrid retrieval over the OmniCare claims table.

    Wraps ragkit's :class:`ragkit.retrieval.HybridRetriever`
    with the claims binding. Every retrieval is scoped to
    the requesting user: ``owner_id`` is passed as an
    equality filter on both search stages, so another
    user's claims are never returned.
    """

    def __init__(self, settings: Any = None, session_factory: Any = None) -> None:
        """Initialize the claims retriever.

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
            spec=CLAIMS_RETRIEVER_SPEC,
            settings=self._settings,
            session_factory=self._session_factory,
        )

    async def retrieve(  # noqa: PLR0913
        self,
        query: str,
        user_id: uuid.UUID,
        n_results: int = 5,
        distance_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Run hybrid search over the calling user's claims only.

        Args:
            query: Natural language query.
            user_id: The authenticated user whose claims are
                searched. The ``owner_id`` equality filter is
                always applied — this is the horizontal
                privilege-escalation guard.
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
            owner_id=str(user_id),
        )
        return as_dicts(results)


async def retrieve_claims_hybrid(  # noqa: PLR0913
    query: str,
    user_id: uuid.UUID,
    n_results: int = 5,
    distance_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Run hybrid search over the calling user's claims only.

    Module-level entry point preserving the historical
    ``app.rag.claims_rag.retrieve_claims_hybrid`` contract:
    any retrieval failure is logged and reported as an empty
    result set rather than raised.

    Args:
        query: Natural language query.
        user_id: The authenticated user whose claims are
            searched. The ``owner_id`` equality filter is
            always applied — this is the horizontal
            privilege-escalation guard.
        n_results: Maximum number of results to return.
        distance_threshold: Maximum L2 distance for the
            vector search part. Defaults to
            ``settings.rag_distance_threshold``.

    Returns:
        List of result dicts, or an empty list on failure.
    """
    try:
        return await ClaimsRetriever().retrieve(
            query,
            user_id=user_id,
            n_results=n_results,
            distance_threshold=distance_threshold,
        )
    except Exception as exc:
        logger.error("Hybrid claims search failed: %s", exc)
        return []
