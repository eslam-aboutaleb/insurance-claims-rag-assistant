"""OmniCare RAG evaluation wiring over ragit.

The evaluation harness (retrieval metrics, answer judges,
the two-phase runner) is domain-agnostic (ragit); this
module is the OmniCare binding: the curated evaluation
dataset grounded in ``sample_policy.md`` and the
``run_evaluation()`` entry point that wires the OmniCare
hybrid retriever and dataset into ragit's runner, so
answer metrics are computed from the same retrieval
context as the retrieval metrics.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from enum import StrEnum
from typing import Any

from app.config import get_settings
from app.domain.policies.retriever import retrieve_hybrid
from ragit.evaluation.dataset import Difficulty, EvalSample
from ragit.evaluation.runner import (
    EvalConfig,
    EvalResult,
    RetrievalFunction,
    main as _ragit_main,
    run_evaluation as _ragit_run_evaluation,
)

logger = logging.getLogger(__name__)


class QuestionCategory(StrEnum):
    """Categories of questions for stratified evaluation."""

    COVERAGE = "coverage"
    LIMITS = "limits"
    EXCLUSIONS = "exclusions"
    DEDUCTIBLES = "deductibles"
    REQUIREMENTS = "requirements"
    GENERAL = "general"


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
    evaluation dataset into ragit's runner. Answer
    metrics are computed from the same retrieval context
    as the retrieval metrics (the runner never re-retrieves).

    Args:
        config: Evaluation configuration. If None, uses defaults
            (context mode with heuristic judge). Requests for the
            legacy "end-to-end" mode are mapped transparently to
            "context" (see below).

    Returns:
        EvalResult with all metrics and per-sample details.
    """
    if config is None:
        config = EvalConfig()

    # ragit 0.3.0 renamed context scoring to mode="context";
    # mode="end-to-end" now requires an injected answer_fn
    # (a real answer generator), which this app does not wire.
    # The app's historical "end-to-end" semantics — scoring the
    # retrieved context against the gold answer — live on as
    # ragit's "context" mode, so map the request transparently.
    # (ragit's EvalConfig is a dataclass, hence dataclasses.replace.)
    if config.mode == "end-to-end":
        config = replace(config, mode="context")

    async def _retrieval_fn(query: str) -> list[dict[str, Any]]:
        return await retrieve_hybrid(query=query, n_results=config.n_results)

    return await _ragit_run_evaluation(
        config,
        _retrieval_fn,
        get_eval_dataset(category=config.category, difficulty=config.difficulty),
        settings=get_settings(),
    )


async def _cli_runner(
    config: EvalConfig,
    _retrieval_fn: RetrievalFunction | None,
    _dataset: list[Any] | None,
    _settings: Any = None,
) -> EvalResult:
    """Run a CLI-parsed evaluation with the OmniCare defaults injected.

    The runner signature is mandated by ragit's CLI
    (``runner(config, retrieval_fn, dataset, settings)``); the
    OmniCare harness derives the dataset and settings from the
    config itself, so the latter three arguments are unused.
    """
    return await run_evaluation(config)


def main() -> None:
    """CLI entry point for running OmniCare RAG evaluation."""
    _ragit_main(runner=_cli_runner)


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
