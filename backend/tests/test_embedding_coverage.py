"""Coverage tests for the app.domain.embeddings adapter.

The implementation lives in :mod:`ragit.embeddings`; these tests pin the shim's compatibility
surface: the factory delegates to the ragit provider registry and the LiteLLM function class is
re-exported unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestEmbedding:
    def test_get_embedding_function_returns_callable(self):
        from app.domain.embeddings import EmbeddingFactory

        func = EmbeddingFactory.get_embedding_function()
        assert callable(func)

    def test_embedding_factory_returns_function_with_model_name(self):
        from app.domain.embeddings import EmbeddingFactory

        with patch("app.domain.embeddings.get_settings") as mock_get:
            mock_settings = MagicMock()
            mock_settings.embedding_provider = "litellm"
            mock_settings.embedding_model = "custom-model"
            mock_get.return_value = mock_settings
            func = EmbeddingFactory.get_embedding_function()
            assert func.model_name == "custom-model"

    def test_factory_returns_the_ragit_litellm_function(self):
        from app.domain.embeddings import EmbeddingFactory, LitellmEmbeddingFunction

        func = EmbeddingFactory.get_embedding_function()

        assert isinstance(func, LitellmEmbeddingFunction)

    def test_litellm_embedding_function_is_reexported(self):
        from app.domain.embeddings import LitellmEmbeddingFunction

        from ragit.embeddings.litellm import (
            LitellmEmbeddingFunction as RagkitFunction,
        )

        assert LitellmEmbeddingFunction is RagkitFunction
