"""OmniCare RAG evaluation wiring over ragkit (ragkit plan 07).

The evaluation harness (retrieval metrics, answer judges,
the two-phase runner) is domain-agnostic (ragkit); this
module is the OmniCare binding: the curated evaluation
dataset grounded in ``sample_policy.md`` and the
``run_evaluation()`` entry point that wires the OmniCare
hybrid retriever and dataset into ragkit's runner, so
answer metrics are computed from the same retrieval
context as the retrieval metrics.

This module replaces the deprecated ``app.rag.eval_dataset``,
``app.rag.eval_runner``, ``app.rag.evaluation``, and
``app.rag.answer_evaluator`` shims (removed in plan 07).
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.domain.policies.retriever import retrieve_hybrid
from ragkit.evaluation.dataset import (
    Difficulty,
    EvalSample,
    QuestionCategory,
)
from ragkit.evaluation.runner import (
    EvalConfig,
    EvalResult,
    RetrievalFunction,
    main as _ragkit_main,
    run_evaluation as _ragkit_run_evaluation,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Curated evaluation dataset grounded in sample_policy.md
# ---------------------------------------------------------------------------

EVAL_DATASET: list[EvalSample] = [
    # --- Water Damage Coverage (Section 1) ---
    EvalSample(
        query="Is water damage from a pipe burst covered?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "Yes, water damage caused by sudden pipe bursts is covered "
            "up to $25,000 with a $500 deductible."
        ),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="What is the maximum payout for water damage?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=("Water damage caused by sudden pipe bursts is covered up to $25,000."),
        category=QuestionCategory.LIMITS,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="How much is the deductible for water damage claims?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=("The deductible for water damage from sudden pipe bursts is $500."),
        category=QuestionCategory.DEDUCTIBLES,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="Does the policy cover gradual water leaks?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "No, gradual leaks are strictly excluded from water damage coverage. "
            "Only sudden pipe bursts are covered."
        ),
        category=QuestionCategory.EXCLUSIONS,
        difficulty=Difficulty.MEDIUM,
    ),
    EvalSample(
        query="Am I covered for flood damage?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "No, flood damage is strictly excluded from the water damage coverage. "
            "Only sudden pipe bursts are covered."
        ),
        category=QuestionCategory.EXCLUSIONS,
        difficulty=Difficulty.MEDIUM,
    ),
    EvalSample(
        query="My bathroom pipe suddenly burst and flooded the floor. Am I covered?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "Yes, water damage caused by sudden pipe bursts is covered up to $25,000 "
            "with a $500 deductible."
        ),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.MEDIUM,
        metadata={"scenario": "real-world"},
    ),
    # --- Personal Property Coverage (Section 2) ---
    EvalSample(
        query="What is covered under personal property protection?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=("Electronics, furniture, and jewelry are covered up to $10,000 total."),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="What is the coverage limit for personal property?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=(
            "Personal property including electronics, furniture, and jewelry is "
            "covered up to $10,000 total."
        ),
        category=QuestionCategory.LIMITS,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="Do I need an appraisal for expensive items?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=("Yes, single items exceeding $2,500 require individual appraisal receipts."),
        category=QuestionCategory.REQUIREMENTS,
        difficulty=Difficulty.MEDIUM,
    ),
    EvalSample(
        query="I want to claim for a $3,000 laptop. What do I need?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=(
            "Since the laptop exceeds $2,500, you will need an individual appraisal "
            "receipt. Electronics are covered under personal property protection "
            "up to $10,000 total."
        ),
        category=QuestionCategory.REQUIREMENTS,
        difficulty=Difficulty.MEDIUM,
        metadata={"scenario": "real-world"},
    ),
    # --- Cross-section / Edge cases ---
    EvalSample(
        query="What types of claims does OmniCare handle?",
        expected_sections=[
            "Section 1: Home Water Damage Coverage",
            "Section 2: Personal Property Protection",
        ],
        gold_answer=(
            "OmniCare handles water damage claims (sudden pipe bursts, up to $25,000) "
            "and personal property claims (electronics, furniture, jewelry, up to $10,000)."
        ),
        category=QuestionCategory.GENERAL,
        difficulty=Difficulty.HARD,
    ),
    EvalSample(
        query="What are all the coverage limits in the policy?",
        expected_sections=[
            "Section 1: Home Water Damage Coverage",
            "Section 2: Personal Property Protection",
        ],
        gold_answer=(
            "The policy has two coverage limits: water damage from sudden pipe bursts "
            "is covered up to $25,000, and personal property (electronics, furniture, "
            "jewelry) is covered up to $10,000 total."
        ),
        category=QuestionCategory.LIMITS,
        difficulty=Difficulty.HARD,
    ),
    EvalSample(
        query="What things are NOT covered by OmniCare?",
        expected_sections=[
            "Section 1: Home Water Damage Coverage",
        ],
        gold_answer=(
            "Gradual leaks and flood damage are explicitly excluded from the water damage coverage."
        ),
        category=QuestionCategory.EXCLUSIONS,
        difficulty=Difficulty.HARD,
    ),
]


def get_eval_dataset(
    category: QuestionCategory | None = None,
    difficulty: Difficulty | None = None,
) -> list[EvalSample]:
    """Return the evaluation dataset, optionally filtered.

    Args:
        category: If provided, only return samples of this category.
        difficulty: If provided, only return samples of this difficulty.

    Returns:
        List of EvalSample matching the filters.
    """
    dataset = EVAL_DATASET
    if category is not None:
        dataset = [s for s in dataset if s.category == category]
    if difficulty is not None:
        dataset = [s for s in dataset if s.difficulty == difficulty]
    return dataset


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
    "Difficulty",
    "EVAL_DATASET",
    "EvalConfig",
    "EvalResult",
    "EvalSample",
    "QuestionCategory",
    "RetrievalFunction",
    "get_eval_dataset",
    "main",
    "run_evaluation",
]
