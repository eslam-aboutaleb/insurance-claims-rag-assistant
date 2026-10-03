"""Re-export shim for the ragkit embedding seam.

The implementation moved to :mod:`ragkit.embeddings` (ragkit
plan 02). This module keeps the historical import path working
until plan 07 removes the shims.

The factory delegates to
:func:`ragkit.embeddings.registry.get_embedding_function`
with the application settings, so provider selection now flows
through the ragkit provider registry. ``get_settings`` remains
importable from this module so tests can patch it here.
"""

from __future__ import annotations

from app.config import get_settings
from ragkit.embeddings.litellm import LitellmEmbeddingFunction
from ragkit.embeddings.registry import get_embedding_function as _get_embedding_function

__all__ = ["EmbeddingFactory", "LitellmEmbeddingFunction"]


class EmbeddingFactory:
    """
    Factory for creating embedding functions.

    The factory reads the embedding configuration from the centralized
    Pydantic settings and returns a ready-to-use embedding function
    instance. This decouples the embedding provider choice from the
    rest of the RAG pipeline.
    """

    @classmethod
    def get_embedding_function(cls):
        """Create and return an embedding function.

        Delegates to the ragkit embedding registry, which selects the
        provider from ``settings.embedding_provider`` (defaulting to
        the LiteLLM provider) and configures it with the API key and
        model name from the application settings.

        Returns:
            An embedding function instance ready for use with the
            retriever and ingestion pipelines.
        """
        return _get_embedding_function(get_settings())
