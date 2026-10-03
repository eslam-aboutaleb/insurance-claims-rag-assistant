"""Chunker provider registry.

Registers the chunking strategies under their provider
names. Registration happens here (rather than via decorators
in the class modules) so importing this module alone yields a
fully populated registry without import cycles.
"""

from __future__ import annotations

from ragkit.chunking.base import Chunker
from ragkit.chunking.markdown import MarkdownSectionChunker
from ragkit.chunking.sliding_window import SlidingWindowChunker
from ragkit.registry import Registry

chunker_registry: Registry[Chunker] = Registry()

chunker_registry.register("sliding-window")(SlidingWindowChunker)
chunker_registry.register("markdown")(MarkdownSectionChunker)
