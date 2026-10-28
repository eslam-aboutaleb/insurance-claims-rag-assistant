"""Settings-driven model factory for ADK's LiteLLM adapter."""

from google.adk.models.lite_llm import LiteLlm

from app.core.config import Settings


class LlmFactory:
    """Create models for a provider or OpenAI-compatible gateway via LiteLLM."""

    @staticmethod
    def create_model(settings: Settings) -> LiteLlm:
        """Resolve routing and credentials entirely from application settings."""
        options = {}
        if settings.litellm_api_key.get_secret_value():
            options["api_key"] = settings.litellm_api_key.get_secret_value()
        if settings.litellm_api_base:
            options["api_base"] = settings.litellm_api_base
        if settings.llm_temperature is not None:
            options["temperature"] = settings.llm_temperature
        if settings.llm_max_tokens is not None:
            options["max_tokens"] = settings.llm_max_tokens
        return LiteLlm(
            model=settings.llm_model,
            **options,
        )
