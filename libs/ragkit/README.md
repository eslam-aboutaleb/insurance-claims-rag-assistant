# ragkit

Domain-agnostic RAG toolkit: embeddings, chunking,
vector stores, retrieval, ingestion, evaluation, and
jobs.

ragkit is the extracted RAG subsystem of the OmniCare
Financial assistant, generalized so any application
can plug its own domain bindings into the same
machinery. The OmniCare adapters
(`backend/app/domain/`) are the reference
implementation.

## Install

```bash
pip install -e libs/ragkit
```

The example app additionally needs FastAPI and
uvicorn:

```bash
pip install -e libs/ragkit fastapi uvicorn
```

## Configure

ragkit reads its configuration through the
`RagSettings` protocol — your existing pydantic
`Settings` already works when it exposes the fields
below (OmniCare's `app.config.Settings` does):

| Field | Used by |
|-------|---------|
| `embedding_model` | embedder model name |
| `rag_distance_threshold` | retrieval distance cutoff |
| `vector_store_provider` | store provider name (`pgvector`, `memory`) |
| `openai_api_key` / `openai_api_base` | LiteLLM provider |
| `embedding_drain_interval_seconds` / `embedding_drain_batch_size` | drainer |
| `job_stale_after_seconds` / `worker_id` | job outbox |
| `ingest_on_startup` / `database_url` | ingestion |

## Plug a provider

Providers register themselves in ragkit's registries —
swap a provider without editing ragkit:

```python
from ragkit.embeddings import embedding_registry
from ragkit.embeddings.base import EmbeddingFunction


@embedding_registry.register("my-provider")
class MyEmbeddingProvider:
    def __init__(self, settings) -> None:
        self._settings = settings

    def create(self) -> EmbeddingFunction:
        return MyEmbedder(self._settings.openai_api_key)
```

The same pattern applies to vector stores
(`ragkit.stores.vector_store_registry`).

## Ingest

```python
from ragkit.chunking import MarkdownSectionChunker, SlidingWindowChunker
from ragkit.ingestion import FileDocumentSource, IngestionPipeline

pipeline = IngestionPipeline(
    store=my_store,
    embedder=my_embedder,
    chunker=MarkdownSectionChunker(SlidingWindowChunker()),
    session_factory=my_session_factory,
    settings=my_settings,
    version_id_key="policy_version_id",
    source_id_key="policy_id",
)
count = await pipeline.run(FileDocumentSource("docs/policy.md"), my_version_store)
```

The pipeline compares the source hash and the chunker
snapshot against the active version, so re-running
ingestion on an unchanged document is a no-op.

## Retrieve

```python
from ragkit.retrieval import HybridRetriever, RetrieverSpec

retriever = HybridRetriever(
    spec=RetrieverSpec(
        table_name="documents",
        id_field="id",
        text_field="text",
        metadata_fields=["section", "source"],
    ),
    settings=my_settings,
    session_factory=my_session_factory,
)
results = await retriever.retrieve("query", n_results=5)
```

Retrieval combines pgvector similarity search with
PostgreSQL full-text search, fused with Reciprocal
Rank Fusion.

## Evaluate

```python
from ragkit.evaluation.runner import EvalConfig, run_evaluation

result = await run_evaluation(
    EvalConfig(n_results=5),
    my_retrieval_fn,
    my_labeled_dataset,
    settings=my_settings,
)
print(result.retrieval_metrics)  # recall@K, precision@K, MRR
```

Answer metrics reuse the same retrieval context as
the retrieval metrics, so evaluation never
re-retrieves.

## Run the drainer

```python
from ragkit.jobs.drainer import main

main(store, processor, interval=5.0, batch_size=10, worker_id="worker-1")
```

The drainer claims jobs with `FOR UPDATE SKIP
LOCKED`, so multiple drainers run concurrently.

## Reference: the OmniCare bindings

The OmniCare adapters show the reference wiring:

- `backend/app/domain/policies/` — policy retrieval,
  versioned ingestion, and the `query_policy` tool
- `backend/app/domain/claims/` — owner-scoped claims
  retrieval, the embedding-job outbox, and the
  `search_claims` tool
- `backend/app/domain/embeddings.py` — embedding and
  vector-store factory wiring
- `backend/app/domain/evaluation.py` — the curated
  evaluation dataset and `run_evaluation`
- `backend/app/agent/registry.py` — the agent tool
  registry

## Example

`examples/minimal_app.py` is a complete FastAPI app
using ragkit with zero OmniCare code — in-memory
store, fake embedder, no database:

```bash
python libs/ragkit/examples/minimal_app.py
curl "http://127.0.0.1:8000/search?query=refund"
```

## Test

```bash
cd libs/ragkit && python -m pytest -q
```
