"""
Procedure playbook tool for the OmniCare AI agent.

This tool loads a domain procedure playbook — the coverage determination
checklist or the intake triage procedure — out of the procedures index, so the
agent gets the procedure as grounded text with provenance instead of the whole
playbook living in the system prompt.

The tool is a plain async function the agent registry can call directly. It uses
the domain adapter ``app.domain.procedures.retrieval`` for retrieval, which is
scoped to a single playbook file so a coverage question can never come back
with triage text attached to it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.domain.procedures.retrieval import retrieve_procedures
from app.domain.procedures.store import get_active_version_record

logger = logging.getLogger(__name__)

_PROCEDURE_CHUNK_LIMIT = 16
"""Chunks pulled per playbook.

Must exceed the chunk count of the largest playbook, or the tool would
silently return a truncated procedure. A playbook that reaches the limit is
logged so the corpus split can be revisited.
"""

_MAX_NAME_ECHO = 64
"""Bound on the requested name echoed back in an error.

The name arrives from the model, so it is untrusted text: the echo keeps the
envelope readable and stops a long name from flooding the response.
"""


@dataclass(frozen=True)
class _ProcedureSpec:
    """One retrievable procedure playbook.

    Attributes:
        name: Canonical procedure name the model invokes.
        source_file: Playbook file in the procedures index.
        topic: Retrieval seed naming the playbook's subject matter.
    """

    name: str
    source_file: str
    topic: str


_PROCEDURES: tuple[_ProcedureSpec, ...] = (
    _ProcedureSpec(
        name="coverage-review",
        source_file="coverage-review.md",
        topic=(
            "coverage determination procedure incident facts covered peril "
            "appraisal threshold coverage cap decision"
        ),
    ),
    _ProcedureSpec(
        name="claim-triage",
        source_file="claim-triage.md",
        topic=(
            "intake triage procedure classify request missing required fields "
            "escalation triggers routing"
        ),
    ),
)

# Normalised spellings the model is likely to produce. The canonical names are
# always accepted; these cover the phrasings that mean the same playbook.
_ALIASES: dict[str, str] = {
    "coverage": "coverage-review",
    "coverage-determination": "coverage-review",
    "coverage-review-procedure": "coverage-review",
    "coveragequestion": "coverage-review",
    "triage": "claim-triage",
    "intake": "claim-triage",
    "claim-intake": "claim-triage",
    "intake-triage": "claim-triage",
    "claim-triage-procedure": "claim-triage",
}

_BY_NAME: dict[str, _ProcedureSpec] = {spec.name: spec for spec in _PROCEDURES}


def _normalise(value: str) -> str:
    """Fold a requested procedure name into its lookup key."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _resolve(name: str) -> _ProcedureSpec | None:
    """Resolve a requested name to a playbook, or ``None`` when unknown."""
    key = _normalise(name)
    canonical = _ALIASES.get(key, key)
    return _BY_NAME.get(canonical)


def _sort_key(result: dict[str, Any]) -> tuple[int, int]:
    """Return the document-order sort key for one chunk.

    Hybrid retrieval ranks by relevance, which would hand the model the
    playbook's steps out of order. The chunk indexes restore the order the
    playbook was written in.
    """
    metadata = result.get("metadata") or {}
    chunk_index = metadata.get("chunk_index")
    sub_chunk_index = metadata.get("sub_chunk_index")
    return (
        chunk_index if isinstance(chunk_index, int) else 1 << 30,
        sub_chunk_index if isinstance(sub_chunk_index, int) else 1 << 30,
    )


def _dedupe_sources(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one citation per section, in the order the sections appear."""
    sources: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    for result in results:
        metadata = result.get("metadata") or {}
        section = metadata.get("section") or "Procedure"
        if section in seen_sections:
            continue
        seen_sections.add(section)
        distance = result.get("distance")
        sources.append(
            {
                "section": section,
                "source": metadata.get("source") or "procedure playbook",
                "relevance_score": round(distance, 4) if distance is not None else None,
            }
        )
    return sources


def _error_envelope(name: str, message: str) -> dict[str, Any]:
    """Build a structured error envelope that leaks nothing internal."""
    logger.warning("get_procedure rejected request for %r: %s", name[:_MAX_NAME_ECHO], message)
    return {
        "procedure": None,
        "version": None,
        "chunk_count": 0,
        "sources": [],
        "name": name[:_MAX_NAME_ECHO],
        "error": message,
    }


async def get_procedure(name: str) -> dict[str, Any]:
    """Load a domain procedure playbook from the procedures index.

    Use this tool when a decision needs a procedure rather than a policy fact:
    call it before deciding whether a described incident is covered, and before
    routing a message whose intent is unclear. Then follow the returned
    ``procedure`` text step by step, and take every policy number, limit, and
    exclusion from ``query_policy`` rather than from the procedure itself.

    Args:
        name: Procedure to load. One of ``coverage-review`` or
            ``claim-triage``.

    Returns:
        dict: On success, ``procedure`` (the playbook text in section order),
        ``version`` (the active ingestion version it was served from),
        ``chunk_count``, ``sources`` (one citation per section), and ``name``.
        On failure, ``procedure`` is ``None`` and ``error`` explains why.
    """
    requested = (name or "").strip()
    if not requested:
        return _error_envelope(
            name or "",
            "A procedure name is required. Available procedures: "
            f"{', '.join(sorted(_BY_NAME))}.",
        )

    spec = _resolve(requested)
    if spec is None:
        return _error_envelope(
            name,
            f"Unknown procedure '{requested[:_MAX_NAME_ECHO]}'. Available "
            f"procedures: {', '.join(sorted(_BY_NAME))}.",
        )

    record = await get_active_version_record(spec.source_file)
    if record is None:
        return _error_envelope(
            name,
            f"The '{spec.name}' procedure is not in the index yet. Answer from "
            "the policy documents directly and say the procedure is unavailable.",
        )

    results = await retrieve_procedures(
        spec.topic,
        n_results=_PROCEDURE_CHUNK_LIMIT,
        source_file=spec.source_file,
    )
    if not results:
        return _error_envelope(
            name,
            f"The '{spec.name}' procedure could not be read from the index. "
            "Answer from the policy documents directly and say the procedure "
            "is unavailable.",
        )

    results.sort(key=_sort_key)
    if len(results) >= _PROCEDURE_CHUNK_LIMIT:
        # The playbook may have grown past the retrieval window, which would
        # silently truncate the procedure the model is about to follow.
        logger.warning(
            "Procedure '%s' returned %d chunks, the retrieval limit; "
            "the playbook may be truncated.",
            spec.name,
            len(results),
        )

    return {
        "procedure": "\n\n".join(result.get("document", "") for result in results),
        "version": record["version"],
        "chunk_count": len(results),
        "sources": _dedupe_sources(results),
        "name": spec.name,
        "error": None,
    }
