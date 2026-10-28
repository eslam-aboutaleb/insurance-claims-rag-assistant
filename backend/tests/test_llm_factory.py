"""Verify settings drive chat and embedding routing without provisioning code."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.agent.agent import create_agent
from app.agent.models import LlmFactory
from app.core.config import Settings
from app.domain.embeddings import EmbeddingFactory, get_embedding_dimension


@pytest.fixture(autouse=True)
def isolated_model_environment(monkeypatch):
    # Imports may load a local dotenv file; never use real credentials in tests.
    for name in (
        "LITELLM_API_KEY",
        "LITELLM_API_BASE",
        "LITELLM_MODEL",
        "LITELLM_EMBEDDING_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "LLM_MODEL",
        "EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def model_settings(**values):
    # Explicit values isolate these tests from a developer's environment.
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://localhost/omnicare_test",
        **values,
    )


@pytest.mark.parametrize(
    ("model", "base", "key"),
    [
        ("openai/agent-tools", "https://gateway.example/v1", "sk-virtual-test"),
        ("openai/gpt-4o-mini", "", "sk-provider-test"),
        ("anthropic/claude-sonnet-4-5", "", "test-anthropic-key"),
        ("gemini/gemini-2.5-flash", "", "test-google-key"),
    ],
)
def test_chat_factory_routes_from_settings(model, base, key):
    settings = model_settings(llm_model=model, litellm_api_base=base, litellm_api_key=key)
    with patch("app.agent.models.LiteLlm") as adapter:
        result = LlmFactory.create_model(settings)
    assert result is adapter.return_value
    assert adapter.call_args.kwargs["model"] == model
    assert adapter.call_args.kwargs.get("api_base", "") == base
    assert adapter.call_args.kwargs["api_key"] == key


def test_chat_factory_preserves_zero_temperature():
    settings = model_settings(llm_temperature=0.0, llm_max_tokens=256)
    with patch("app.agent.models.LiteLlm") as adapter:
        LlmFactory.create_model(settings)
    assert adapter.call_args.kwargs["temperature"] == 0.0
    assert adapter.call_args.kwargs["max_tokens"] == 256


def test_chat_factory_omits_unset_generation_options():
    settings = model_settings(llm_temperature=None, llm_max_tokens=None)
    with patch("app.agent.models.LiteLlm") as adapter:
        LlmFactory.create_model(settings)
    assert "temperature" not in adapter.call_args.kwargs
    assert "max_tokens" not in adapter.call_args.kwargs


def test_agent_uses_model_factory():
    settings = model_settings()
    with (
        patch("app.agent.agent.LlmFactory.create_model") as factory,
        patch("app.agent.agent.LlmAgent") as agent,
    ):
        create_agent(MagicMock(), settings)
    factory.assert_called_once_with(settings)
    assert agent.call_args.kwargs["model"] is factory.return_value


def test_embedding_factory_routes_to_gateway():
    settings = model_settings(
        litellm_api_base="https://gateway.example/v1",
        litellm_api_key="sk-virtual-test",
        embedding_provider="litellm",
        embedding_model="openai/rag-embedding",
    )
    with patch("app.domain.embeddings.get_settings", return_value=settings):
        embedding = EmbeddingFactory.get_embedding_function()
    with patch("ragnor.embeddings.litellm.litellm.embedding") as adapter:
        adapter.return_value.data = [{"embedding": [0.0] * 1536}]
        result = embedding._embed_sync(["synthetic test"])
    assert len(result[0]) == get_embedding_dimension(settings.embedding_model) == 1536
    assert adapter.call_args.kwargs == {
        "model": "openai/rag-embedding",
        "input": ["synthetic test"],
        "api_key": "sk-virtual-test",
        "api_base": "https://gateway.example/v1",
    }


def test_litellm_environment_settings_drive_both_factories(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-portable-token")
    monkeypatch.setenv("LITELLM_API_BASE", "https://gateway.example/v1")
    monkeypatch.setenv("LITELLM_MODEL", "openai/agent-tools")
    monkeypatch.setenv("LITELLM_EMBEDDING_MODEL", "openai/rag-embedding")
    # Canonical names must win when both new and old names are present.
    monkeypatch.setenv("OPENAI_API_KEY", "old-test-token")
    monkeypatch.setenv("OPENAI_API_BASE", "https://old.example/v1")
    monkeypatch.setenv("LLM_MODEL", "openai/old-model")
    monkeypatch.setenv("EMBEDDING_MODEL", "old-embedding")
    settings = model_settings(embedding_provider="litellm")
    assert settings.litellm_api_key.get_secret_value() == "test-portable-token"
    assert settings.litellm_api_base == "https://gateway.example/v1"
    assert settings.llm_model == "openai/agent-tools"
    assert settings.embedding_model == "openai/rag-embedding"
    with patch("app.agent.models.LiteLlm") as chat:
        LlmFactory.create_model(settings)
    with patch("app.domain.embeddings.get_settings", return_value=settings):
        embedding = EmbeddingFactory.get_embedding_function()
    assert chat.call_args.kwargs["api_key"] == embedding.api_key == "test-portable-token"
    assert chat.call_args.kwargs["api_base"] == embedding.api_base == "https://gateway.example/v1"
    assert embedding.model_name == "openai/rag-embedding"


def test_legacy_environment_settings_still_load(monkeypatch):
    for name in ("LITELLM_API_KEY", "LITELLM_API_BASE", "LITELLM_MODEL", "LITELLM_EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-legacy-token")
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy.example/v1")
    monkeypatch.setenv("LLM_MODEL", "openai/legacy-chat")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/legacy-embedding")
    settings = model_settings()
    assert settings.litellm_api_key.get_secret_value() == "test-legacy-token"
    assert settings.litellm_api_base == "https://legacy.example/v1"
    assert settings.llm_model == "openai/legacy-chat"
    assert settings.embedding_model == "openai/legacy-embedding"


def test_explicit_settings_override_environment(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-environment-key")
    monkeypatch.setenv("LITELLM_MODEL", "openai/environment-model")
    settings = model_settings(
        litellm_api_key="test-explicit-key", llm_model="openai/explicit-model"
    )
    assert settings.litellm_api_key.get_secret_value() == "test-explicit-key"
    assert settings.llm_model == "openai/explicit-model"


def test_legacy_process_environment_overrides_canonical_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_API_KEY=test-file-key\n")
    monkeypatch.setenv("OPENAI_API_KEY", "test-process-key")
    settings = Settings(_env_file=env_file, database_url="postgresql+asyncpg://localhost/test")
    assert settings.litellm_api_key.get_secret_value() == "test-process-key"


def test_chat_factory_leaves_native_credential_resolution_to_litellm():
    settings = model_settings(litellm_api_key="", litellm_api_base="")
    with patch("app.agent.models.LiteLlm") as adapter:
        LlmFactory.create_model(settings)
    assert "api_key" not in adapter.call_args.kwargs
    assert "api_base" not in adapter.call_args.kwargs


def test_embedding_provider_overrides_do_not_change_chat_configuration():
    settings = model_settings(
        litellm_api_key="test-chat-key",
        litellm_api_base="https://chat.example/v1",
        embedding_api_key="test-embedding-key",
        embedding_api_base="",
        embedding_model="cohere/embed-english-v3.0",
    )
    with patch("app.domain.embeddings.get_settings", return_value=settings):
        embedding = EmbeddingFactory.get_embedding_function()
    assert embedding.api_key == "test-embedding-key"
    assert embedding.api_base == ""
    assert embedding.model_name == "cohere/embed-english-v3.0"
    with patch("app.agent.models.LiteLlm") as chat:
        LlmFactory.create_model(settings)
    assert chat.call_args.kwargs["api_key"] == "test-chat-key"
    assert chat.call_args.kwargs["api_base"] == "https://chat.example/v1"


def test_custom_embedding_dimensions_reach_vector_store():
    from app.domain.embeddings import get_vector_store

    settings = model_settings(embedding_model="custom-alias", embedding_dimension=1024)
    with (
        patch("app.domain.embeddings.get_settings", return_value=settings),
        patch("app.domain.embeddings._ragnor_get_vector_store") as store,
    ):
        assert get_embedding_dimension() == 1024
        assert get_embedding_dimension("custom-alias") == 1024
        assert get_embedding_dimension("text-embedding-3-small") == 1536
        get_vector_store("test_vectors", "id")
    assert store.call_args.kwargs["embedding_dim"] == 1024


def test_credentials_are_masked_in_settings_and_validation_errors():
    secrets = ("test-chat-secret-value", "test-embedding-secret-value")
    settings = model_settings(litellm_api_key=secrets[0], embedding_api_key=secrets[1])
    representations = repr(settings) + str(settings.model_dump()) + settings.model_dump_json()
    assert all(secret not in representations for secret in secrets)
    with pytest.raises(ValidationError) as error:
        model_settings(litellm_api_key=secrets[0], embedding_dimension=-1)
    assert secrets[0] not in str(error.value)


@pytest.mark.parametrize(
    "url", ["file:///tmp/model", "https://user:password@host/v1", "https://host/v1?key=secret"]
)
def test_reject_credentials_and_invalid_protocol_in_endpoint(url):
    with pytest.raises(ValidationError):
        model_settings(litellm_api_base=url)
    with pytest.raises(ValidationError):
        model_settings(embedding_api_base=url)


@pytest.mark.parametrize("kind", ["policy", "claims", "procedures"])
def test_retrieval_uses_embedding_credentials_and_custom_dimension(kind):
    from app.domain.claims.retriever import ClaimsRetriever
    from app.domain.policies.retriever import PolicyRetriever
    from app.domain.procedures.retrieval import ProcedureRetriever

    retrievers = {
        "policy": PolicyRetriever,
        "claims": ClaimsRetriever,
        "procedures": ProcedureRetriever,
    }
    settings = model_settings(
        litellm_api_key="test-chat-key",
        embedding_api_key="test-retrieval-key",
        embedding_api_base="https://embedding.example/v1",
        embedding_model=f"custom-{kind}-alias",
        embedding_dimension=1024,
    )
    retriever = retrievers[kind](settings=settings)._retriever
    assert retriever._embed_fn.api_key == "test-retrieval-key"
    assert retriever._embed_fn.api_base == "https://embedding.example/v1"
    assert retriever._store.embedding_dim == 1024
