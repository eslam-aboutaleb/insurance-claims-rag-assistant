"""Evaluation dataset types for RAG systems.

Moved from ``backend/app/rag/eval_dataset.py`` (ragkit
extraction plan 05). This module is **types only**: the
curated OmniCare sample data stays in the host application
(domain test data, not library code) and constructs these
``EvalSample`` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class QuestionCategory(StrEnum):
    """Categories of questions for stratified evaluation."""

    COVERAGE = "coverage"
    LIMITS = "limits"
    EXCLUSIONS = "exclusions"
    DEDUCTIBLES = "deductibles"
    REQUIREMENTS = "requirements"
    GENERAL = "general"


class Difficulty(StrEnum):
    """Difficulty levels for evaluation queries."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


@dataclass
class EvalSample:
    """A single evaluation sample with query, expected retrieval, and gold answer."""

    query: str
    expected_sections: list[str]
    gold_answer: str
    category: QuestionCategory
    difficulty: Difficulty
    metadata: dict[str, Any] = field(default_factory=dict)
