"""
Claims search tool for the OmniCare AI agent.

This tool allows the AI agent to search through a user's
claims using hybrid search (vector similarity + PostgreSQL
full-text search) to answer questions about claim status,
claim history, etc.

The tool is implemented as a plain async function that the
agent registry can call directly. It uses the domain adapter
``app.domain.claims.retriever`` for retrieval, which always
scopes the search to the authenticated user.
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.claims.retriever import retrieve_claims_hybrid

logger = logging.getLogger(__name__)


async def search_claims(query: str, n_results: int = 5) -> list[dict[str, Any]]:
    """
    Search claims using hybrid search (vector + keyword).

    Use this tool when the user asks about their claims,
    claim status, claim history, or any question that
    requires looking up claim information.

    The search combines:
    - Vector similarity search (pgvector) for semantic matching
    - PostgreSQL full-text search (tsvector) for keyword matching
    - Reciprocal Rank Fusion (RRF) to merge results from both methods

    Args:
        query: Natural language query to search claims.
            Example: "What is the status of my water damage claim?"
        n_results: Maximum number of results to return (default: 5).

    Returns:
        list[dict]: List of search results, each containing:
            - document: The text content of the matching claim
            - metadata: Dict with claim_id, policy_number, claim_type,
              status, amount, description, owner_id
            - distance: Vector distance (lower is closer)
            - _rrf_score: Combined RRF relevance score

        Returns list with error/message dict if:
            - User is not authenticated (returns unauthorized error)
            - No results found (returns message)
            - Search fails (returns error message)

    Examples:
        >>> # Search for claim status
        >>> results = await search_claims("water damage claim status")
        >>> if results and "error" not in results[0]:
        ...     print(f"Found {len(results)} claims")
        ...     for r in results:
        ...         print(f"Claim: {r['metadata']['claim_id']}")
        ...         print(f"Status: {r['metadata']['status']}")
    """
    # Resolved at call time so importing this module never
    # cycles through app.agent (the agent imports this tool
    # at module level).
    from app.agent.context import current_user_id  # noqa: PLC0415

    try:
        user_uuid = current_user_id.get()
    except LookupError:
        logger.error("current_user_id not found in context during claims search.")
        return [{"error": "Unauthorized claim search."}]

    try:
        results = await retrieve_claims_hybrid(query=query, user_id=user_uuid, n_results=n_results)
        if not results:
            return [{"message": "No relevant claims found matching your search."}]
        return results
    except Exception as exc:
        logger.exception("Error during hybrid claims search: %s", exc)
        return [{"error": "Unable to search claims at this time. Please try again later."}]
