"""
RAG evaluation harness for the OmniCare backend.

Re-export shim for :mod:`ragkit.evaluation.retrieval`
(ragkit plan 05). The harness is domain-agnostic and
lives in ragkit; this shim keeps ``app.rag.evaluation``
importable until plan 07 removes the shims.
"""

from ragkit.evaluation.retrieval import (
    LabeledQuery,
    RagEvaluationHarness,
    RetrievalMetrics,
)

__all__ = [
    "LabeledQuery",
    "RagEvaluationHarness",
    "RetrievalMetrics",
]
