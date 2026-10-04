"""OmniCare embedding and vector-store wiring over ragkit (ragkit plan 07).

The embedding provider registry and the vector-store ABCs
are domain-agnostic (ragkit); this module is the OmniCare
wiring: it selects the provider from the application
settings and injects the app's session factory, embedding
function, and embedding dimension — the dependencies
ragkit, being domain-agnostic, cannot import itself.

This module replaces the deprecated ``app.rag.embedding``,
``app.rag.embedding_dimensions``, ``app.rag.vector_store``,
and ``app.rag.pgvector_store`` shims (removed in plan 07).
"""

from __future__ import annotations

from app.config import get_settings
from app.database import async_session_factory
from ragkit.embeddings import (
    EmbeddingFunction,
    LitellmEmbeddingFunction,
    get_embedding_dimension,
    register_dimension,
)
from ragkit.embeddings.registry import (
    get_embedding_function as _get_embedding_function,
)
from ragkit.stores import VectorStore, get_vector_store as _ragkit_get_vector_store

__all__ = [
    "EmbeddingFactory",
    "LitellmEmbeddingFunction",
    "get_embedding_dimension",
    "get_vector_store",
    "register_dimension",
]


class EmbeddingFactory:
    """
    Factory for creating embedding functions.

    The factory reads the embedding configuration from the centralized
    Pydantic settings and returns a ready-to-use embedding function
    instance. This decouples the embedding provider choice from the
    rest of the RAG pipeline.
    """

    @classmethod
    def get_embedding_function(cls) -> EmbeddingFunction:
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


def get_vector_store(
    table_name: str,
    id_field: str = "id",
    embedding_dim: int | None = None,
) -> VectorStore:
    """Factory function to create a vector store instance.

    Args:
        table_name: Name of the database table.
        id_field: Name of the ID column.
        embedding_dim: Dimension of the embedding vectors. Defaults
            to the dimension configured for the active embedding model.

    Returns:
        Configured VectorStore instance based on provider setting.
    """
    settings = get_settings()
    return _ragkit_get_vector_store(
        table_name=table_name,
        id_field=id_field,
        embedding_dim=(embedding_dim if embedding_dim is not None else get_embedding_dimension()),
        settings=settings,
        session_factory=async_session_factory,
        embedding_fn=EmbeddingFactory.get_embedding_function(),
    )
