"""
Unit tests for the RAG ingestion and hybrid retrieval modules using the vector store abstraction.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.database import async_session_factory
from app.domain.claims.ingest import ingest_all_claims, ingest_claim
from app.domain.claims.retriever import retrieve_claims_hybrid
from app.domain.embeddings import EmbeddingFactory, LitellmEmbeddingFunction
from app.domain.policies.ingestion import (
    POLICY_CHUNKER_SNAPSHOT_VERSION,
    PolicyVersionStore,
    chunk_policy_document,
    ingest_policy,
)
from app.domain.policies.retriever import retrieve_hybrid
from ragit.types import SearchResult


def test_chunk_policy_document(tmp_path):
    md_file = tmp_path / "test.md"
    md_file.write_text(
        "## Section 1\n\nThis is a test policy.\n\n## Section 2\n\nAnother section.",
        encoding="utf-8",
    )
    chunks = chunk_policy_document(str(md_file))
    assert len(chunks) >= 2
    assert chunks[0]["metadata"]["section"] == "Section 1"
    assert "test policy" in chunks[0]["text"].lower()


@pytest.mark.asyncio
async def test_retrieve_hybrid_exception():
    mock_retriever = AsyncMock()
    mock_retriever.retrieve.side_effect = Exception("DB error")

    with patch("app.domain.policies.retriever.HybridRetriever", return_value=mock_retriever):
        res = await retrieve_hybrid("test")
        assert res == []


@pytest.mark.asyncio
async def test_retrieve_hybrid_success():
    mock_result = SearchResult(
        id="chunk-1",
        document="mock document",
        metadata={
            "section": "Mock Section",
            "source": "mock.md",
            "chunk_index": 0,
            "sub_chunk_index": 0,
        },
        distance=0.5,
        rrf_score=0.8,
    )
    mock_retriever = AsyncMock()
    mock_retriever.retrieve.return_value = [mock_result]

    with (
        patch(
            "app.domain.policies.retriever.HybridRetriever", return_value=mock_retriever
        ) as mock_cls,
    ):
        res = await retrieve_hybrid("test", n_results=3, distance_threshold=0.9)

    assert len(res) == 1
    assert res[0]["document"] == "mock document"
    assert res[0]["distance"] == 0.5
    assert res[0]["_rrf_score"] == 0.8
    # The adapter binds the retriever to the policy chunk table.
    spec = mock_cls.call_args[1]["spec"]
    assert spec.table_name == "policy_chunks"
    assert mock_cls.call_args[1]["session_factory"] is async_session_factory
    mock_retriever.retrieve.assert_awaited_once_with("test", n_results=3, distance_threshold=0.9)

    # Without an explicit threshold the settings default is forwarded.
    settings = get_settings()
    with patch("app.domain.policies.retriever.HybridRetriever", return_value=mock_retriever):
        await retrieve_hybrid("test")
    mock_retriever.retrieve.assert_awaited_with(
        "test", n_results=5, distance_threshold=settings.rag_distance_threshold
    )


@pytest.mark.asyncio
async def test_embedding_factory():
    fn = EmbeddingFactory.get_embedding_function()
    assert isinstance(fn, LitellmEmbeddingFunction)
    res = await fn(["test 1", "test 2"])
    assert len(res) == 2
    assert len(res[0]) == 1536


@pytest.mark.asyncio
async def test_embedding_function_embed_query():
    fn = EmbeddingFactory.get_embedding_function()
    result = await fn.embed_query("test query")
    assert len(result) == 1536


@pytest.mark.asyncio
async def test_embedding_function_embed_documents():
    fn = EmbeddingFactory.get_embedding_function()
    result = await fn.embed_documents(["doc1", "doc2"])
    assert len(result) == 2
    assert len(result[0]) == 1536


@pytest.mark.asyncio
async def test_ingest_policy_success(tmp_path):
    """ingest_policy() wires the ragit pipeline to the policy tables."""
    md_file = tmp_path / "policy.md"
    md_file.write_text("# Policy\n\nContent.", encoding="utf-8")

    mock_store = MagicMock()
    mock_embedder = MagicMock()
    mock_source = MagicMock()
    mock_pipeline = AsyncMock()
    mock_pipeline.run.return_value = 7

    with (
        patch("app.domain.embeddings.get_vector_store", return_value=mock_store) as mock_get_store,
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=mock_embedder,
        ),
        patch(
            "app.domain.policies.ingestion.FileDocumentSource", return_value=mock_source
        ) as mock_source_cls,
        patch(
            "app.domain.policies.ingestion.IngestionPipeline", return_value=mock_pipeline
        ) as mock_pipeline_cls,
    ):
        count = await ingest_policy(policy_path=str(md_file))

    assert count == 7
    mock_source_cls.assert_called_once_with(str(md_file))
    mock_get_store.assert_called_once_with(table_name="policy_chunks", id_field="id")

    pipeline_kwargs = mock_pipeline_cls.call_args[1]
    assert pipeline_kwargs["store"] is mock_store
    assert pipeline_kwargs["embedder"] is mock_embedder
    assert pipeline_kwargs["session_factory"] is async_session_factory
    assert pipeline_kwargs["settings"] is get_settings()
    assert pipeline_kwargs["version_id_key"] == "policy_version_id"
    assert pipeline_kwargs["source_id_key"] == "policy_id"
    # The adapter reports the historical chunker version so stored
    # snapshots keep comparing equal (the chunker_version trap).
    assert pipeline_kwargs["chunker"].version == POLICY_CHUNKER_SNAPSHOT_VERSION

    run_args, run_kwargs = mock_pipeline.run.call_args
    assert run_args[0] is mock_source
    assert isinstance(run_args[1], PolicyVersionStore)
    assert run_kwargs["lock_key"] == str(md_file)


@pytest.mark.asyncio
async def test_ingest_policy_defaults_to_configured_path():
    """ingest_policy() falls back to settings.policy_file_path."""
    settings = get_settings()
    mock_source = MagicMock()
    mock_pipeline = AsyncMock()
    mock_pipeline.run.return_value = 0

    with (
        patch(
            "app.domain.policies.ingestion.FileDocumentSource", return_value=mock_source
        ) as mock_source_cls,
        patch("app.domain.policies.ingestion.IngestionPipeline", return_value=mock_pipeline),
    ):
        count = await ingest_policy()

    assert count == 0
    mock_source_cls.assert_called_once_with(settings.policy_file_path)


@pytest.mark.asyncio
async def test_ingest_policy_returns_zero_without_configured_path():
    """ingest_policy() returns 0 when no policy path is configured."""
    stub_settings = MagicMock()
    stub_settings.policy_file_path = ""

    with (
        patch("app.domain.policies.ingestion.get_settings", return_value=stub_settings),
        patch("app.domain.policies.ingestion.FileDocumentSource") as mock_source_cls,
        patch("app.domain.policies.ingestion.IngestionPipeline") as mock_pipeline_cls,
    ):
        count = await ingest_policy()

    assert count == 0
    mock_source_cls.assert_not_called()
    mock_pipeline_cls.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_policy_skips_missing_file():
    """ingest_policy() returns 0 and does not crash when file is missing."""
    mock_store = AsyncMock()

    with patch("app.domain.embeddings.get_vector_store", return_value=mock_store):
        count = await ingest_policy(policy_path="/nonexistent/path/policy.md")
        assert count == 0
        mock_store.upsert.assert_not_called()


# Claims RAG tests


@pytest.mark.asyncio
async def test_claims_rag_hybrid():
    test_user_id = uuid.uuid4()

    mock_store = AsyncMock()
    mock_store.hybrid_search.return_value = [
        {
            "document": "Claim TEST-123: Water Damage - Pipe burst",
            "metadata": {
                "claim_id": "TEST-123",
                "policy_number": "POL-123",
                "claim_type": "Water Damage",
                "status": "Pending",
                "amount": 1500.0,
            },
            "distance": 0.5,
            "_rrf_score": 0.8,
        }
    ]

    with (
        patch("ragit.retrieval.retriever.get_vector_store", return_value=mock_store),
        patch("ragit.retrieval.retriever.get_embedding_function") as mock_embed,
    ):
        mock_embed.return_value = AsyncMock(return_value=[[0.1] * 1536])
        results = await retrieve_claims_hybrid("kitchen pipe", test_user_id)
        assert len(results) > 0
        assert results[0]["metadata"]["claim_id"] == "TEST-123"
        mock_store.hybrid_search.assert_called_once()


@pytest.mark.asyncio
async def test_claims_rag_exception_handling():
    test_user_id = uuid.uuid4()

    with patch("ragit.retrieval.retriever.get_vector_store") as mock_factory:
        mock_store = AsyncMock()
        mock_factory.return_value = mock_store
        mock_store.hybrid_search.side_effect = Exception("DB error")

        with patch("ragit.retrieval.retriever.get_embedding_function") as mock_embed:
            mock_embed.return_value = AsyncMock(return_value=[[0.1] * 1536])
            error_results = await retrieve_claims_hybrid("kitchen", test_user_id)
            assert error_results == []


@pytest.mark.asyncio
async def test_ingest_claim():
    test_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    test_user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_store = AsyncMock()

    with (
        patch("app.domain.claims.ingest.get_vector_store", return_value=mock_store),
        patch("app.domain.claims.ingest.EmbeddingFactory.get_embedding_function") as mock_embed,
    ):
        mock_embed.return_value = AsyncMock(return_value=[[0.1] * 1536])
        await ingest_claim(
            claim_uuid=test_id,
            claim_id="CLM-1",
            owner_id=test_user_id,
            claim_type="Water",
            description="Pipe burst",
            policy_number="POL-1",
            status="Open",
            amount=1000.0,
        )
        mock_store.upsert.assert_called_once()
        call_docs = mock_store.upsert.call_args[1]["documents"]
        assert call_docs[0]["id"] == str(test_id)
        assert call_docs[0]["metadata"]["claim_id"] == "CLM-1"


@pytest.mark.asyncio
async def test_ingest_all_claims():
    mock_claim = MagicMock()
    mock_claim.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_claim.claim_id = "CLM-1"
    mock_claim.owner_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_claim.claim_type = "Water"
    mock_claim.description = "Pipe burst"
    mock_claim.policy_number = "POL-1"
    mock_claim.status = "Open"
    mock_claim.amount = 1000.0

    with patch("app.domain.claims.ingest.async_session_factory") as mock_factory:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_claim]
        mock_session.execute.return_value = mock_result
        mock_factory.return_value.__aenter__.return_value = mock_session

        with patch("app.domain.claims.ingest.ingest_claim", new_callable=AsyncMock) as mock_ingest:
            await ingest_all_claims()
            mock_ingest.assert_called_once()
            call_kwargs = mock_ingest.call_args[1]
            assert call_kwargs["claim_uuid"] == mock_claim.id
            assert call_kwargs["claim_id"] == mock_claim.claim_id


# --- Defect 2: submit_claim_internal enqueues EmbeddingJob ---


@pytest.mark.asyncio
async def test_submit_claim_internal_enqueues_embedding_job():
    """submit_claim_internal() creates an EmbeddingJob with the correct claim_uuid."""
    from app.agent.tools.submit_claim import EmbeddingJob as EmbeddingJobModule
    from app.agent.tools.submit_claim import submit_claim_internal
    from app.agent.context import current_user_id

    user_uuid = uuid.UUID("00000000-0000-0000-0000-000000000003")
    current_user_id.set(user_uuid)

    new_claim = MagicMock()
    new_claim.id = uuid.UUID("00000000-0000-0000-0000-000000000004")
    new_claim.claim_id = "CLM-TEST"
    new_claim.owner_id = user_uuid
    new_claim.claim_type = "Water Damage"
    new_claim.description = "Pipe burst"
    new_claim.policy_number = "POL-1092"
    new_claim.status = "Submitted"
    new_claim.amount = 1500.0

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()

    fixed_uuid = uuid.UUID("00000000-0000-0000-0000-000000000004")

    with (
        patch("app.agent.tools.submit_claim.async_session_factory") as mock_factory,
        patch.object(EmbeddingJobModule, "__init__", return_value=None) as mock_job_init,
        patch("app.agent.tools.submit_claim.uuid.uuid4", return_value=fixed_uuid),
    ):
        mock_factory.return_value.__aenter__.return_value = mock_session

        # Patch Claim constructor to return our mock
        with patch("app.agent.tools.submit_claim.Claim", return_value=new_claim):
            result = await submit_claim_internal(
                policy_number="POL-1092",
                claim_type="Water Damage",
                amount=1500.0,
                description="Pipe burst",
                user_uuid=user_uuid,
            )

    assert result["success"] is True
    assert result["confirmation_id"] == "CLM-00000000"
    mock_job_init.assert_called_once()
    call_kwargs = mock_job_init.call_args[1]
    assert call_kwargs["claim_uuid"] == new_claim.id
    assert call_kwargs["claim_id"] == "CLM-TEST"
    assert call_kwargs["owner_id"] == user_uuid
    assert call_kwargs["claim_type"] == "Water Damage"
