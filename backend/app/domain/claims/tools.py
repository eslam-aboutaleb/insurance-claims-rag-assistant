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


def _to_context_result(result: dict[str, Any]) -> dict[str, Any]:
    """Project one raw retrieval row onto the LLM-facing envelope.

    ``owner_id`` is the caller's own raw user UUID — already implied by the
    retrieval scoping — and ``_rrf_score`` plus ``distance`` are internal
    ranking signals; none of them help the model answer, so they are dropped.

    Args:
        result: Raw retrieval row with ``document``, ``metadata``,
            ``distance``, and ``_rrf_score`` keys.

    Returns:
        dict: The row with ``document``, sanitized ``metadata``, and a
        ``citation`` string in the same shape ``get_claim_status`` returns.
    """
    metadata = dict(result.get("metadata") or {})
    metadata.pop("owner_id", None)
    metadata.pop("_rrf_score", None)
    claim_id = metadata.get("claim_id", "")
    policy_number = metadata.get("policy_number", "")
    return {
        "document": result.get("document", ""),
        "metadata": metadata,
        "citation": f"Claim {claim_id} in the OmniCare claims system (policy {policy_number}).",
    }


async def search_claims(query: str, n_results: int = 5) -> dict[str, Any]:
    """
    Search claims using hybrid search (vector + keyword).

    Use this tool when the user asks about their claims, claim status,
    claim history, or mentions a claim without giving its ID.

    The search combines:
    - Vector similarity search (pgvector) for semantic matching
    - PostgreSQL full-text search (tsvector) for keyword matching
    - Reciprocal Rank Fusion (RRF) to merge results from both methods

    Args:
        query: Natural language query to search claims.
            Example: "What is the status of my water damage claim?"
        n_results: Maximum number of results to return (default: 5).

    Returns:
        dict: An envelope with the keys:
            - results: List of matches, each with ``document``, ``metadata``
              (claim_id, policy_number, claim_type, status, amount,
              description), and ``citation``.
            - count: Number of matches in ``results``.
            - message: Set (with empty ``results``) when the search succeeded
              but found no matching claim.
            - error: Set (with empty ``results``) when the lookup itself
              failed or the caller is not authenticated.

    Examples:
        >>> # Search for claim status
        >>> envelope = await search_claims("water damage claim status")
        >>> if envelope["error"]:
        ...     print("The lookup failed:", envelope["error"])
        ... elif envelope["message"]:
        ...     print("No matching claim:", envelope["message"])
        ... else:
        ...     for r in envelope["results"]:
        ...         print(f"Claim: {r['metadata']['claim_id']}")
        ...         print(f"Citation: {r['citation']}")
    """
    # Resolved at call time so importing this module never
    # cycles through app.agent (the agent imports this tool
    # at module level).
    from app.agent.context import current_user_id  # noqa: PLC0415

    try:
        user_uuid = current_user_id.get()
    except LookupError:
        logger.error("current_user_id not found in context during claims search.")
        return {
            "results": [],
            "count": 0,
            "message": None,
            "error": "Unauthorized claim search.",
        }

    try:
        results = await retrieve_claims_hybrid(query=query, user_id=user_uuid, n_results=n_results)
        if not results:
            return {
                "results": [],
                "count": 0,
                "message": "No relevant claims found matching your search.",
                "error": None,
            }
        return {
            "results": [_to_context_result(result) for result in results],
            "count": len(results),
            "message": None,
            "error": None,
        }
    except Exception as exc:
        logger.exception("Error during hybrid claims search: %s", exc)
        return {
            "results": [],
            "count": 0,
            "message": None,
            "error": "Unable to search claims at this time. Please try again later.",
        }
