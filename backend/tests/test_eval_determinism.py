"""
Eval determinism test (ragkit plan 05, validation 7).

``run_evaluation`` moved to ragkit with its retrieval
function and dataset injected. This test drives the
moved runner with a stubbed retrieval function and the
application's curated dataset, and snapshots the
resulting metrics: the formulas moved verbatim, so the
deterministic metric keys must match the pre-move
values exactly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.evaluation import (
    Difficulty,
    QuestionCategory,
    get_eval_dataset,
)
from ragkit.evaluation.runner import (
    EvalConfig,
    run_evaluation,
)


def _settings() -> MagicMock:
    return MagicMock(llm_model="openai/gpt-4o-mini")


async def _stubbed_retrieval_fn(query: str) -> list[dict]:
    """Deterministic retrieval: each question maps to its policy section."""
    if "water" in query.lower():
        return [
            {
                "id": "chunk-1",
                "document": (
                    "Yes, water damage caused by sudden pipe bursts is covered "
                    "up to $25,000 with a $500 deductible."
                ),
                "metadata": {
                    "section": "Section 1: Home Water Damage Coverage",
                    "source": "sample_policy.md",
                    "chunk_id": "chunk-1",
                },
                "distance": 0.1,
                "_rrf_score": 0.9,
            }
        ]
    return [
        {
            "id": "chunk-2",
            "document": "Electronics, furniture, and jewelry are covered up to $10,000 total.",
            "metadata": {
                "section": "Section 2: Personal Property Protection",
                "source": "sample_policy.md",
                "chunk_id": "chunk-2",
            },
            "distance": 0.1,
            "_rrf_score": 0.9,
        }
    ]


@pytest.mark.asyncio
async def test_run_evaluation_metrics_match_pre_move_snapshot():
    """The moved runner reproduces the pre-move metric values."""
    dataset = get_eval_dataset(
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.EASY,
    )
    assert len(dataset) == 2

    result = await run_evaluation(
        EvalConfig(mode="end-to-end", judge="heuristic", n_results=5),
        _stubbed_retrieval_fn,
        dataset,
        settings=_settings(),
    )

    # Retrieval metrics (avg_latency_ms is wall-clock and excluded).
    assert result.retrieval_metrics["recall_at_k"] == 1.0
    assert result.retrieval_metrics["precision_at_k"] == 1.0
    assert result.retrieval_metrics["mrr"] == 1.0
    assert result.retrieval_metrics["total_queries"] == 2
    assert result.retrieval_metrics["errors"] == 0
    assert result.retrieval_metrics["avg_latency_ms"] >= 0.0

    # Answer metrics, computed from the same retrieval context.
    assert result.answer_metrics["avg_faithfulness"] == 1.0
    assert result.answer_metrics["avg_relevance"] == 0.5383
    assert result.answer_metrics["avg_completeness"] == 1.0
    assert result.answer_metrics["avg_conciseness"] == 1.0
    assert result.answer_metrics["avg_overall"] == 0.8845
    assert result.answer_metrics["total_evaluated"] == 2
    assert result.answer_metrics["errors"] == 0

    # Per-sample answer scores are deterministic too.
    by_query = {s["query"]: s for s in result.per_sample_results}
    water = by_query["Is water damage from a pipe burst covered?"]
    property_ = by_query["What is covered under personal property protection?"]
    assert water["answer_overall"] == 0.975
    assert property_["answer_overall"] == 0.7941
    assert water["answer_faithfulness"] == 1.0
    assert property_["answer_faithfulness"] == 1.0


@pytest.mark.asyncio
async def test_run_evaluation_is_deterministic_across_runs():
    """Two runs with the same inputs produce identical metrics."""
    dataset = get_eval_dataset(
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.EASY,
    )

    first = await run_evaluation(
        EvalConfig(mode="end-to-end", judge="heuristic", n_results=5),
        _stubbed_retrieval_fn,
        dataset,
        settings=_settings(),
    )
    second = await run_evaluation(
        EvalConfig(mode="end-to-end", judge="heuristic", n_results=5),
        _stubbed_retrieval_fn,
        dataset,
        settings=_settings(),
    )

    for metrics in (first.retrieval_metrics, second.retrieval_metrics):
        metrics.pop("avg_latency_ms", None)
    assert first.retrieval_metrics == second.retrieval_metrics
    assert first.answer_metrics == second.answer_metrics
    assert first.per_sample_results == second.per_sample_results
