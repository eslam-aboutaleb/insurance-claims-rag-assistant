"""
Re-export shim for the ragkit vector store abstraction.

The ``VectorStore`` interface and the provider registry moved to
``ragkit.stores`` (ragkit extraction plan 03). This module keeps
the historical import path working until plan 07 removes the shims.

The factory below delegates to the ragkit store registry and wires
the OmniCare-specific dependencies (the app's session factory, the
LiteLLM embedding function, and the configured embedding dimension)
that ragkit, being domain-agnostic, cannot import itself.
"""

from app.config import get_settings
from app.database import async_session_factory
from app.rag.embedding import EmbeddingFactory
from app.rag.embedding_dimensions import get_embedding_dimension
from ragkit.stores import VectorStore, get_vector_store as _ragkit_get_vector_store


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
