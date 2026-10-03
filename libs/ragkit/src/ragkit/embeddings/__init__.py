"""Embedding seam: function protocol, LiteLLM provider, registry, dimensions."""

from __future__ import annotations

from ragkit.embeddings.base import EmbeddingFunction
from ragkit.embeddings.dimensions import (
    FALLBACK_DIMENSION,
    get_embedding_dimension,
    register_dimension,
)
from ragkit.embeddings.litellm import LitellmEmbeddingFunction
from ragkit.embeddings.registry import (
    LiteLLMEmbeddingProvider,
    embedding_registry,
    get_embedding_function,
)

__all__ = [
    "EmbeddingFunction",
    "FALLBACK_DIMENSION",
    "LitellmEmbeddingFunction",
    "LiteLLMEmbeddingProvider",
    "embedding_registry",
    "get_embedding_dimension",
    "get_embedding_function",
    "register_dimension",
]
