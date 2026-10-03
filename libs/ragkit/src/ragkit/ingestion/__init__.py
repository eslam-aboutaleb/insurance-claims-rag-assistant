"""Ingestion seam: sources, snapshot versioning, locking, pipeline."""

from __future__ import annotations

from ragkit.ingestion.locking import advisory_lock
from ragkit.ingestion.pipeline import (
    IngestionPipeline,
    VersionRecord,
    VersionStore,
)
from ragkit.ingestion.sources import (
    DocumentSource,
    FileDocumentSource,
    RawDocument,
)
from ragkit.ingestion.versioning import (
    RETRIEVAL_SCHEMA_VERSION,
    IngestionSnapshot,
    build_snapshot,
    snapshot_matches,
)

__all__ = [
    "DocumentSource",
    "FileDocumentSource",
    "IngestionPipeline",
    "IngestionSnapshot",
    "RETRIEVAL_SCHEMA_VERSION",
    "RawDocument",
    "VersionRecord",
    "VersionStore",
    "advisory_lock",
    "build_snapshot",
    "snapshot_matches",
]
