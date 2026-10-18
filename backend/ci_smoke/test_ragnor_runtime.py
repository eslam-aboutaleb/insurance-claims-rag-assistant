"""Lightweight import check for Ragnor APIs used by the backend."""

from ragnor.embeddings import get_embedding_dimension
from ragnor.ingestion import FileDocumentSource, IngestionPipeline
from ragnor.jobs import JobPayload, claim_jobs, enqueue_job, process_pending_jobs
from ragnor.retrieval import HybridRetriever, RetrieverSpec, as_dicts


def test_ragnor_import_surface_is_available() -> None:
    """Verify the service's shared RAG primitives resolve from the new package."""
    imported = (
        get_embedding_dimension,
        FileDocumentSource,
        IngestionPipeline,
        JobPayload,
        claim_jobs,
        enqueue_job,
        process_pending_jobs,
        HybridRetriever,
        RetrieverSpec,
        as_dicts,
    )
    if any(symbol is None for symbol in imported):
        raise RuntimeError("Ragnor exported an unexpected empty symbol")
