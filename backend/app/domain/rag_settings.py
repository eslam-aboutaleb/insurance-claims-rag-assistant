"""Translate application configuration at the ragnor dependency boundary."""

from typing import Any

from ragnor.embeddings import get_embedding_dimension, register_dimension

from app.core.config import Settings


class RagnorSettingsAdapter:
    """Expose Ragnor's credential protocol without vendor names in Settings."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        dimension = settings.embedding_dimension
        if dimension is not None:
            register_dimension(settings.embedding_model, dimension)
        elif settings.embedding_model.startswith("openai/"):
            register_dimension(
                settings.embedding_model,
                get_embedding_dimension(settings.embedding_model.removeprefix("openai/")),
            )

    @property
    def openai_api_key(self) -> str:
        key = self._settings.embedding_api_key
        if key is None:
            key = self._settings.litellm_api_key
        return key.get_secret_value()

    @property
    def openai_api_base(self) -> str:
        base = self._settings.embedding_api_base
        return self._settings.litellm_api_base if base is None else base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)
