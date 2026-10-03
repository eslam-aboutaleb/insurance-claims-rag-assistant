"""
Tests for the ragkit pgvector store.

Moved from ``backend/tests/test_pgvector_store.py`` (ragkit
extraction plan 03). The store now receives its session
factory and embedding function through constructor injection
instead of module-level singletons, so the tests inject mocks
at construction time rather than patching module attributes.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ragkit.stores.pgvector import PgVectorStore


def _mock_row(values: dict) -> MagicMock:
    """Build a row double that only supports ``__getitem__``, like a RowMapping."""
    row = MagicMock()
    row.__getitem__ = lambda _self, key: values[key]
    return row


def _mocked_session(rows: list[MagicMock]) -> AsyncMock:
    """Build a session whose queries return ``rows``."""
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    session.execute.return_value = result
    return session


def _make_store(
    session: AsyncMock,
    *,
    table_name: str = "policy_chunks",
    id_field: str = "id",
    embedding_fn: AsyncMock | None = None,
) -> tuple[PgVectorStore, MagicMock]:
    """Wire a store to a mocked session factory (and optional embedder)."""
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    store = PgVectorStore(
        table_name=table_name,
        id_field=id_field,
        embedding_dim=1536,
        session_factory=factory,
        embedding_fn=embedding_fn,
    )
    return store, factory


@pytest.mark.asyncio
async def test_hybrid_search_with_empty_embedding():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "test doc",
                    "section": "Test",
                    "source": "test.md",
                    "chunk_index": 0,
                    "sub_chunk_index": 0,
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )
    embed_fn = AsyncMock(return_value=[[0.1] * 1536])

    store, _factory = _make_store(mock_session, embedding_fn=embed_fn)

    result = await store.hybrid_search(
        query="test",
        embedding=[],
        n_results=5,
        threshold=1.3,
        text_field="text",
        metadata_fields=["section", "source"],
    )
    assert len(result) == 1
    assert result[0]["document"] == "test doc"


@pytest.mark.asyncio
async def test_hybrid_search_with_extra_where():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "test doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session, table_name="claims", embedding_fn=AsyncMock())

    result = await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="description",
        metadata_fields=["claim_id"],
        extra_where="owner_id = :owner_id",
        extra_params={"owner_id": "user-123"},
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_count_with_extra_where():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 5
    mock_session.execute.return_value = mock_result

    store, _factory = _make_store(mock_session, table_name="claims")

    count = await store.count(
        extra_where="owner_id = :owner_id", extra_params={"owner_id": "user-123"}
    )
    assert count == 5


@pytest.mark.asyncio
async def test_hybrid_search_empty_metadata_fields_no_trailing_comma():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "test doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
        metadata_fields=[],
    )
    stmt = mock_session.execute.call_args[0][0]
    sql = str(stmt)
    assert ", ," not in sql


@pytest.mark.asyncio
async def test_hybrid_search_with_empty_metadata_fields():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "test doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    result = await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
        metadata_fields=[],
    )
    assert len(result) == 1
    assert result[0]["metadata"] == {}


@pytest.mark.asyncio
async def test_count_without_extra_where():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 10
    mock_session.execute.return_value = mock_result

    store, _factory = _make_store(mock_session)

    count = await store.count()
    assert count == 10


@pytest.mark.asyncio
async def test_hybrid_search_metadata_field_populated():
    row = _mock_row(
        {
            "id": "1",
            "document": "test doc",
            "section": "Test Section",
            "source": "test.md",
            "chunk_index": 0,
            "sub_chunk_index": 0,
            "distance": 0.5,
            "rrf_score": 0.8,
        }
    )
    row.__contains__ = lambda _self, key: (
        key
        in {
            "id",
            "document",
            "section",
            "source",
            "chunk_index",
            "sub_chunk_index",
            "distance",
            "rrf_score",
        }
    )
    mock_session = _mocked_session([row])

    store, _factory = _make_store(mock_session)

    result = await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
        metadata_fields=["section", "source"],
    )
    assert len(result) == 1
    assert result[0]["metadata"]["section"] == "Test Section"
    assert result[0]["metadata"]["source"] == "test.md"


@pytest.mark.asyncio
async def test_count_exception_handling():
    factory = MagicMock()
    factory.return_value.__aenter__.side_effect = Exception("DB error")

    store = PgVectorStore(
        table_name="policy_chunks",
        id_field="id",
        embedding_dim=1536,
        session_factory=factory,
    )

    count = await store.count()
    assert count == 0


@pytest.mark.asyncio
async def test_upsert_with_empty_documents():
    factory = MagicMock()

    store = PgVectorStore(
        table_name="policy_chunks",
        id_field="id",
        embedding_dim=1536,
        session_factory=factory,
    )

    await store.upsert([])
    factory.return_value.__aenter__.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_with_extra_fields():
    mock_session = AsyncMock()
    mock_session.execute.return_value = None

    store, _factory = _make_store(mock_session, table_name="claims")

    await store.upsert(
        documents=[
            {
                "id": "claim-1",
                "text": "test claim",
                "embedding": [0.1] * 1536,
                "claim_id": "CLM-1",
                "owner_id": "user-123",
            }
        ],
        extra_fields={"status": "Pending"},
    )
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_hybrid_search_keyword_only_distance_is_none():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "keyword only doc",
                    "distance": None,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    result = await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
    )
    assert len(result) == 1
    assert result[0]["distance"] is None


@pytest.mark.asyncio
async def test_hybrid_search_vector_distance_zero_preserved():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "identical vector doc",
                    "distance": 0.0,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    result = await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
    )
    assert len(result) == 1
    assert result[0]["distance"] == 0.0


@pytest.mark.asyncio
async def test_upsert_commits_transactions():
    mock_session = AsyncMock()
    mock_session.execute.return_value = None

    store, _factory = _make_store(mock_session)

    await store.upsert(
        documents=[
            {
                "id": "chunk-1",
                "text": "test chunk",
                "embedding": [0.1] * 1536,
            }
        ]
    )
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_hybrid_search_vector_row_number_tie_break():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
    )
    stmt = mock_session.execute.call_args[0][0]
    sql = str(stmt)
    assert (
        "ORDER BY\n                               embedding <-> cast(:embedding as vector),\n                               id"
        in sql
    )


@pytest.mark.asyncio
async def test_hybrid_search_keyword_row_number_tie_break():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
    )
    stmt = mock_session.execute.call_args[0][0]
    sql = str(stmt)
    assert (
        "ORDER BY\n                               ts_rank(tsvector, plainto_tsquery('english', :query)) DESC,\n                               id"
        in sql
    )


@pytest.mark.asyncio
async def test_hybrid_search_outer_order_by_tie_break():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "id": "1",
                    "document": "doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session)

    await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
    )
    stmt = mock_session.execute.call_args[0][0]
    sql = str(stmt)
    assert "ORDER BY rrf_score DESC, id" in sql


@pytest.mark.asyncio
async def test_hybrid_search_tie_break_uses_custom_id_field():
    mock_session = _mocked_session(
        [
            _mock_row(
                {
                    "chunk_id": "1",
                    "document": "doc",
                    "distance": 0.5,
                    "rrf_score": 0.8,
                }
            )
        ]
    )

    store, _factory = _make_store(mock_session, id_field="chunk_id")

    await store.hybrid_search(
        query="test",
        embedding=[0.1] * 1536,
        n_results=5,
        threshold=1.3,
        text_field="text",
    )
    stmt = mock_session.execute.call_args[0][0]
    sql = str(stmt)
    assert (
        "ORDER BY\n                               embedding <-> cast(:embedding as vector),\n                               chunk_id"
        in sql
    )
    assert (
        "ORDER BY\n                               ts_rank(tsvector, plainto_tsquery('english', :query)) DESC,\n                               chunk_id"
        in sql
    )
    assert "ORDER BY rrf_score DESC, chunk_id" in sql
