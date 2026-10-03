"""Markdown section-aware chunking.

Extracted from ``chunk_policy_document`` in
``backend/app/rag/ingest.py`` (ragkit plan 02). Splits a
Markdown document on ``##`` section headers to preserve
semantic boundaries, then delegates each reconstructed
section to an inner chunker (typically
:class:`ragkit.chunking.sliding_window.SlidingWindowChunker`)
to produce overlapping sub-chunks.

The file-reading logic that used to precede the parsing
moves to the plan-04 ``FileDocumentSource``; this chunker
operates on text and takes the source name as a constructor
argument.

Danger detail for plan 06: the ingestion snapshot in
``backend/app/rag/ingest.py`` stores ``chunker_version="v1"``.
This chunker reports ``version = "section-aware-v1"``; the
plan-06 domain adapter must map ``"section-aware-v1"`` back
to ``"v1"`` in the snapshot (or backfill) so existing
deployments do not re-ingest everything. Plan 06 owns that
mapping.
"""

from __future__ import annotations

import re
from typing import Any

from ragkit.chunking.base import Chunker
from ragkit.types import Chunk


class MarkdownSectionChunker:
    """Chunker that splits Markdown on ``##`` section headers.

    Each section is reconstructed with its H2 header
    (``f"## {title}\\n\\n{body}"``) so the section title is
    part of the chunk text, then passed to the inner chunker.
    """

    def __init__(self, inner: Chunker, source_name: str = "document") -> None:
        """
        Args:
            inner: Chunker applied to each reconstructed section.
            source_name: Source identifier recorded in chunk metadata
                (the host application derives it from the document
                path; plan 04's ``FileDocumentSource`` supplies it).
        """
        self._inner = inner
        self._source_name = source_name

    def chunk(self, text: str) -> list[Chunk]:
        """Parse a Markdown document into structured, overlapping chunks.

        Args:
            text: The Markdown document text.

        Returns:
            Chunks whose metadata carries the frozen keys
            ``chunk_id``, ``section``, ``source``,
            ``chunk_index``, and ``sub_chunk_index``.
        """
        # Split on Markdown H2 headers to preserve section boundaries. The
        # regex captures the header title in group 1, allowing us to
        # reconstruct the document as alternating title/body pairs.
        sections = re.split(r"(?m)^##\s+(.*)$", text)

        preamble = sections[0].strip()
        parsed_sections: list[dict[str, str]] = []
        if preamble:
            title_match = re.search(r"^#\s+(.+)$", preamble, re.MULTILINE)
            preamble_title = title_match.group(1).strip() if title_match else "Introduction"
            parsed_sections.append({"title": preamble_title, "content": preamble})

        for i in range(1, len(sections), 2):
            title = sections[i].strip()
            body = sections[i + 1].strip() if i + 1 < len(sections) else ""
            parsed_sections.append({"title": title, "content": body})

        chunks: list[Chunk] = []
        global_chunk_idx = 0

        for section in parsed_sections:
            section_title = section["title"]
            section_text = section["content"]

            # Reconstruct the full section text with its H2 header so that
            # the section title is included in the chunk content for better
            # retrieval context.
            full_text = f"## {section_title}\n\n{section_text}"
            sub_chunks = self._inner.chunk(full_text)

            for sub_idx, sub_chunk in enumerate(sub_chunks):
                chunks.append(
                    Chunk(
                        id=sub_chunk.id,
                        text=sub_chunk.text,
                        metadata={
                            "chunk_id": f"chunk_{global_chunk_idx}",
                            "section": section_title,
                            "source": self._source_name,
                            "chunk_index": global_chunk_idx,
                            "sub_chunk_index": sub_idx,
                        },
                    )
                )
                global_chunk_idx += 1

        return chunks

    @property
    def version(self) -> str:
        """Version tag for ingestion snapshot comparison.

        Plan 06 maps this back to the historical ``"v1"`` in the
        ingestion snapshot; see the module docstring.
        """
        return "section-aware-v1"

    @property
    def config(self) -> dict[str, Any]:
        """Inner chunker tunables for ingestion snapshots."""
        return dict(self._inner.config)
