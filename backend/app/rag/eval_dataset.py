"""
Curated evaluation dataset for the OmniCare RAG system.

The dataset **types** (``EvalSample``, ``Difficulty``,
``QuestionCategory``) live in ragkit (ragkit plan 05);
the curated OmniCare sample data below is domain test
data and stays in the application, constructing ragkit's
``EvalSample`` objects.

Contains labeled question-answer pairs grounded in the sample_policy.md document,
with expected relevant sections and gold-standard answers for measuring both
retrieval quality and answer generation quality.

Each entry includes:
  - query: A realistic user question
  - expected_sections: Policy sections that should be retrieved
  - gold_answer: A reference answer grounded in policy text
  - category: Question type for stratified reporting
  - difficulty: easy/medium/hard for granular analysis
"""

from __future__ import annotations

from ragkit.evaluation.dataset import (
    Difficulty,
    EvalSample,
    QuestionCategory,
)

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
