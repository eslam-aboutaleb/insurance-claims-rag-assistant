"""OmniCare embedding and vector-store wiring over ragnor.

The embedding provider registry and the vector-store ABCs
are domain-agnostic (ragnor); this module is the OmniCare
wiring: it selects the provider from the application
settings and injects the app's session factory, embedding
function, and embedding dimension — the dependencies
ragnor, being domain-agnostic, cannot import itself.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.domain.rag_settings import RagnorSettingsAdapter
from ragnor.embeddings import (
    EmbeddingFunction,
    LitellmEmbeddingFunction,
    get_embedding_dimension as _ragnor_embedding_dimension,
    register_dimension,
)
from ragnor.embeddings.registry import (
    get_embedding_function as _get_embedding_function,
)
from ragnor.stores import VectorStore, get_vector_store as _ragnor_get_vector_store

__all__ = [
    "EmbeddingFactory",
    "LitellmEmbeddingFunction",
    "get_embedding_dimension",
    "get_vector_store",
    "register_dimension",
]


def get_embedding_dimension(model: str | None = None) -> int:
    """Resolve the configured model and optional alias dimension override."""
    settings = get_settings()
    selected_model = model or settings.embedding_model
    if selected_model == settings.embedding_model and settings.embedding_dimension is not None:
        return settings.embedding_dimension
    return _ragnor_embedding_dimension(selected_model.removeprefix("openai/"))


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

        Delegates to the ragnor embedding registry, which selects the
        provider from ``settings.embedding_provider`` (defaulting to
        the LiteLLM provider) and configures it with the API key and
        model name from the application settings.

        Returns:
            An embedding function instance ready for use with the
            retriever and ingestion pipelines.
        """
        return _get_embedding_function(RagnorSettingsAdapter(get_settings()))


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
    return _ragnor_get_vector_store(
        table_name=table_name,
        id_field=id_field,
        embedding_dim=(embedding_dim if embedding_dim is not None else get_embedding_dimension()),
        settings=RagnorSettingsAdapter(settings),
        session_factory=async_session_factory,
        embedding_fn=EmbeddingFactory.get_embedding_function(),
    )
