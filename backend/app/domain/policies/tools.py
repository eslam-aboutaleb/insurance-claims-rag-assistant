"""
Policy RAG tool for the OmniCare AI agent.

This tool allows the AI agent to search through policy
documents using hybrid search (vector similarity + PostgreSQL
full-text search) to answer customer questions about insurance
policies, coverage, claims process, etc.

The tool is implemented as a plain async function that the
agent registry can call directly. It uses the domain adapter
``app.domain.policies.retriever`` for retrieval.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import get_settings
from app.domain.policies.retriever import retrieve_hybrid

logger = logging.getLogger(__name__)


async def query_policy(query: str) -> dict[str, Any]:
    """Searches OmniCare insurance policy documents to answer coverage questions.

    Uses hybrid retrieval: pgvector similarity search combined with PostgreSQL
    full-text search using Reciprocal Rank Fusion.

    Use this tool whenever the user asks about:
    - What is or is not covered under a policy
    - Coverage limits or maximum payout amounts
    - Deductibles or out-of-pocket amounts
    - Policy exclusions or conditions
    - Any general policy or insurance question

    The tool returns relevant policy text sections with citations. Always
    base your answer on the returned ``answer_context`` — do not add details
    that are not present in the context.

    Args:
        query (str): The user's policy coverage question or search topic.
            Example: "water damage from pipe burst"

    Returns:
        dict with keys:
            - ``answer_context`` (str): Concatenated relevant policy sections
              to use as grounding context in your answer.
            - ``sources`` (list[dict]): Unique source citations, each with
              ``section`` (str), ``source`` (str filename), and
              ``relevance_score`` (float, lower is more relevant).
            - ``chunks_found`` (int): Number of relevant chunks retrieved.
    """
    settings = get_settings()

    results = await retrieve_hybrid(
        query=query,
        n_results=5,
        distance_threshold=settings.rag_distance_threshold,
    )

    if not results:
        return {
            "answer_context": (
                "No relevant policy information found for this query. "
                "The user may be asking about something not covered in the policy documents, "
                "or should contact OmniCare support directly."
            ),
            "sources": [],
            "chunks_found": 0,
        }

    context_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    seen_sections: set[str] = set()

    for result in results:
        doc_text = result["document"]
        metadata = result["metadata"]
        distance = result["distance"]

        section = metadata.get("section")
        if not section:
            heading_match = re.search(r"^##\s+(.+)$", doc_text, re.MULTILINE)
            section = heading_match.group(1) if heading_match else "General Policy"

        source_file = metadata.get("source")
        if not source_file:
            source_file = "sample_policy.md"

        context_parts.append(f"[{section}]:\n{doc_text}")

        # Deduplicate citations — multiple overlapping sub-chunks from the
        # same section should not generate duplicate source entries.
        if section not in seen_sections:
            source_entry: dict[str, Any] = {
                "section": section,
                "source": source_file,
            }
            if distance is not None:
                source_entry["relevance_score"] = round(distance, 4)
            sources.append(source_entry)
            seen_sections.add(section)

    return {
        "answer_context": "\n\n---\n\n".join(context_parts),
        "sources": sources,
        "chunks_found": len(results),
    }
