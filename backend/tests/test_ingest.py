"""
Tests for app.rag.ingest -- the thin adapter over ragkit.

The advisory lock and the ingestion logic moved to
:class:`ragkit.ingestion.IngestionPipeline` (ragkit
plan 04) and are tested in ``libs/ragkit/tests``
(``test_ingestion_locking.py`` and
``test_ingestion_pipeline.py``). These tests verify
the adapter wiring only: that ``ingest_policy`` builds
the pipeline, binds the policy file as the document
source, and passes the policy path as the advisory-lock
key so concurrent ingestion of the same policy stays
serialized.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.ingest import PolicyVersionStore, ingest_policy


@pytest.mark.asyncio
async def test_ingest_policy_runs_pipeline_with_advisory_lock_key():
    """ingest_policy() runs the pipeline with the policy path as lock key.

    The advisory lock is acquired because ``lock_key`` is
    passed to ``pipeline.run``; the lock itself is
    implemented and tested in ragkit.
    """
    policy_path = "/tmp/policy.md"

    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock(return_value=7)
    mock_source = MagicMock()

    with (
        patch("app.rag.ingest.IngestionPipeline", return_value=mock_pipeline),
        patch("app.rag.ingest.FileDocumentSource", return_value=mock_source),
        patch("app.rag.ingest.get_vector_store"),
        patch("app.rag.ingest.EmbeddingFactory"),
    ):
        result = await ingest_policy(policy_path=policy_path)

    assert result == 7

    mock_pipeline.run.assert_awaited_once()
    args, kwargs = mock_pipeline.run.call_args
    # The source and the version store are positional arguments;
    # the advisory-lock key is passed as a keyword argument.
    assert args[0] is mock_source
    assert isinstance(args[1], PolicyVersionStore)
    assert kwargs.get("lock_key") == policy_path


@pytest.mark.asyncio
async def test_ingest_policy_builds_source_from_the_policy_path():
    """The document source reads the policy file at ``policy_path``."""
    policy_path = "/tmp/policy.md"

    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock(return_value=0)

    with (
        patch("app.rag.ingest.IngestionPipeline", return_value=mock_pipeline),
        patch("app.rag.ingest.FileDocumentSource") as mock_source_cls,
        patch("app.rag.ingest.get_vector_store"),
        patch("app.rag.ingest.EmbeddingFactory"),
    ):
        await ingest_policy(policy_path=policy_path)

    mock_source_cls.assert_called_once_with(policy_path)


@pytest.mark.asyncio
async def test_ingest_policy_uses_configured_path_when_none_given():
    """ingest_policy() falls back to settings.policy_file_path."""
    policy_path = "/tmp/configured-policy.md"

    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock(return_value=0)
    mock_source = MagicMock()
    mock_settings = MagicMock()
    mock_settings.policy_file_path = policy_path

    with (
        patch("app.rag.ingest.IngestionPipeline", return_value=mock_pipeline),
        patch("app.rag.ingest.FileDocumentSource", return_value=mock_source),
        patch("app.rag.ingest.get_vector_store"),
        patch("app.rag.ingest.EmbeddingFactory"),
        patch("app.rag.ingest.get_settings", return_value=mock_settings),
    ):
        await ingest_policy()

    mock_pipeline.run.assert_awaited_once()
    _, kwargs = mock_pipeline.run.call_args
    assert kwargs.get("lock_key") == policy_path
