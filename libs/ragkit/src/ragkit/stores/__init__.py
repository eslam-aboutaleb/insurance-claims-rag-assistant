"""Vector store abstractions and providers for ragkit."""

from ragkit.stores.base import EmbeddingFunction, VectorStore
from ragkit.stores.memory import InMemoryVectorStore
from ragkit.stores.pgvector import PgVectorStore
from ragkit.stores.registry import get_vector_store, vector_store_registry

__all__ = [
    "EmbeddingFunction",
    "InMemoryVectorStore",
    "PgVectorStore",
    "VectorStore",
    "get_vector_store",
    "vector_store_registry",
]
