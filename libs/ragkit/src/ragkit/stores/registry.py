"""Vector store registry and factory.

Providers register themselves under a name (``pgvector``,
``memory``) and are looked up case-insensitively through the
generic :class:`ragkit.registry.Registry`. The factory
:func:`get_vector_store` replaces the if/else chain the host
application used before the extraction.
"""

from __future__ import annotations

from ragkit.config import RagSettings
from ragkit.db.session import SessionProvider
from ragkit.registry import Registry
from ragkit.stores.base import EmbeddingFunction, VectorStore

vector_store_registry: Registry[VectorStore] = Registry[VectorStore]()
"""Registry of vector store provider classes."""


def get_vector_store(  # noqa: PLR0913, PLR0917
    table_name: str,
    id_field: str = "id",
    embedding_dim: int | None = None,
    settings: RagSettings | None = None,
    session_factory: SessionProvider | None = None,
    embedding_fn: EmbeddingFunction | None = None,
) -> VectorStore:
    """Create a vector store from the registry.

    The provider class is read from ``settings.vector_store_provider``
    (default ``"pgvector"``) and instantiated with the remaining
    arguments, so host applications inject their session factory,
    embedding function, and embedding dimension through the factory
    instead of the store importing host modules.

    Args:
        table_name: Name of the database table (pgvector provider).
        id_field: Name of the primary key column.
        embedding_dim: Dimensionality of the embedding vectors.
        settings: Host settings. ``vector_store_provider`` selects
            the provider; ``database_url`` is used by the pgvector
            store to open its own sessions when no session factory
            is injected.
        session_factory: Session provider injected into the store.
        embedding_fn: Embedding function injected into the store for
            automatic query embedding.

    Returns:
        Configured VectorStore instance based on the provider setting.

    Raises:
        KeyError: If no provider is registered under the configured
            name. The message lists the registered names.
    """
    provider = "pgvector"
    if settings is not None:
        provider = getattr(settings, "vector_store_provider", "pgvector")
    store_class = vector_store_registry.get(provider)
    return store_class(
        table_name=table_name,
        id_field=id_field,
        embedding_dim=embedding_dim,
        session_factory=session_factory,
        settings=settings,
        embedding_fn=embedding_fn,
    )
