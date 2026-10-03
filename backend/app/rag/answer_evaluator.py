"""
Answer quality evaluation for the OmniCare RAG pipeline.

Re-export shim for :mod:`ragkit.evaluation.answer`
(ragkit plan 05). The evaluators are domain-agnostic
and live in ragkit; the LLM judge reads its model
from the ``RagSettings`` the runner passes in. This
shim keeps ``app.rag.answer_evaluator`` importable
until plan 07 removes the shims.

Provides two evaluation strategies:
  1. **LLM-as-Judge**: uses the configured LLM (via LiteLLM) to score
     answer quality on multiple dimensions (faithfulness, relevance,
     completeness).
  2. **Heuristic scoring**: fast, deterministic keyword/overlap scoring
     that works offline without LLM API calls.
"""

from ragkit.evaluation.answer import (
    AnswerMetrics,
    AnswerScore,
    evaluate_answer_heuristic,
    evaluate_answer_llm,
)

__all__ = [
    "AnswerMetrics",
    "AnswerScore",
    "evaluate_answer_heuristic",
    "evaluate_answer_llm",
]
