"""Coverage tests for the ragkit evaluation dataset types.

The dataset **types** moved to ``ragkit.evaluation.dataset``
(ragkit extraction plan 05); the curated OmniCare sample
data stays in ``backend/app/rag/eval_dataset.py`` and its
filtering tests remain in the backend suite.
"""

from __future__ import annotations

from ragkit.evaluation.dataset import (
    Difficulty,
    EvalSample,
    QuestionCategory,
)


class TestEvalDatasetTypes:
    def test_eval_sample_defaults(self):
        sample = EvalSample(
            query="Is water damage covered?",
            expected_sections=["Section 1"],
            gold_answer="Yes.",
            category=QuestionCategory.COVERAGE,
            difficulty=Difficulty.EASY,
        )
        assert sample.metadata == {}
        assert sample.category.value == "coverage"
        assert sample.difficulty.value == "easy"

    def test_question_category_values(self):
        assert [c.value for c in QuestionCategory] == [
            "coverage",
            "limits",
            "exclusions",
            "deductibles",
            "requirements",
            "general",
        ]

    def test_difficulty_values(self):
        assert [d.value for d in Difficulty] == ["easy", "medium", "hard"]

    def test_eval_sample_is_a_str_enum_member(self):
        sample = EvalSample(
            query="q",
            expected_sections=[],
            gold_answer="g",
            category=QuestionCategory.LIMITS,
            difficulty=Difficulty.HARD,
        )
        assert sample.category == "limits"
        assert sample.difficulty == "hard"
