"""Re-export shim for the ragkit embedding dimension registry.

The implementation moved to
:mod:`ragkit.embeddings.dimensions` (ragkit plan 02). This
module keeps the historical import path working until plan 07
removes the shims.

``get_embedding_dimension()`` still resolves the configured
model from the application settings when no explicit model is
passed, then delegates to the ragkit dimension registry, which
remains the single source of truth shared by ingestion,
retrieval, and the vector stores.
"""

from __future__ import annotations

from ragkit.embeddings.dimensions import (
    get_embedding_dimension as _get_embedding_dimension,
    register_dimension,
)

__all__ = ["get_embedding_dimension", "register_dimension"]


def get_embedding_dimension(model: str | None = None) -> int:
    """Return the vector dimension for an embedding model.

    Args:
        model: The embedding model name. Defaults to
            ``settings.embedding_model``.

    Returns:
        The dimension, or the fallback when the model is not in the known
        table.
    """
    if model is None:
        from app.config import settings  # noqa: PLC0415

        model = settings.embedding_model

    return _get_embedding_dimension(model)
