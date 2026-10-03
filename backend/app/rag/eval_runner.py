"""
RAG evaluation runner for OmniCare Financial.

Shim over :mod:`ragkit.evaluation.runner` (ragkit plan
05). The runner itself is domain-agnostic and takes its
retrieval function and dataset as injected dependencies;
this adapter supplies the OmniCare defaults — the hybrid
retriever and the curated evaluation dataset — so
``run_evaluation()`` keeps working with no arguments.

Orchestrates end-to-end evaluation of the RAG pipeline by running the
curated test dataset through retrieval and answer generation, then
computing metrics across all dimensions.

Supports two modes:
  - **retrieval-only**: Evaluates retrieval quality (Recall@K, Precision@K, MRR)
  - **end-to-end**: Evaluates both retrieval and answer quality (faithfulness,
    relevance, completeness, conciseness)

Can be run as a CLI script or imported for use in tests and the eval API.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.rag.eval_dataset import get_eval_dataset
from app.rag.retriever import retrieve_hybrid
from ragkit.evaluation.runner import (
    EvalConfig,
    EvalResult,
    RetrievalFunction,
    main as _ragkit_main,
    run_evaluation as _ragkit_run_evaluation,
)

logger = logging.getLogger(__name__)


async def run_evaluation(config: EvalConfig | None = None) -> EvalResult:
    """Run a complete RAG evaluation.

    Wires the OmniCare hybrid retriever and the curated
    evaluation dataset into ragkit's runner. Answer
    metrics are computed from the same retrieval context
    as the retrieval metrics (the runner never re-retrieves).

    Args:
        config: Evaluation configuration. If None, uses defaults
            (end-to-end mode with heuristic judge).

    Returns:
        EvalResult with all metrics and per-sample details.
    """
    if config is None:
        config = EvalConfig()

    async def _retrieval_fn(query: str) -> list[dict[str, Any]]:
        return await retrieve_hybrid(query=query, n_results=config.n_results)

    return await _ragkit_run_evaluation(
        config,
        _retrieval_fn,
        get_eval_dataset(category=config.category, difficulty=config.difficulty),
        settings=get_settings(),
    )


async def _cli_runner(
    config: EvalConfig,
    retrieval_fn: RetrievalFunction | None,
    dataset: list[Any] | None,
    settings: Any = None,
) -> EvalResult:
    """Run a CLI-parsed evaluation with the OmniCare defaults injected."""
    return await run_evaluation(config)


def main() -> None:
    """CLI entry point for running OmniCare RAG evaluation."""
    _ragkit_main(runner=_cli_runner)


__all__ = [
    "EvalConfig",
    "EvalResult",
    "RetrievalFunction",
    "main",
    "run_evaluation",
]
