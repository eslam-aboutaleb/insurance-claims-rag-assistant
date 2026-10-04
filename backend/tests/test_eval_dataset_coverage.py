"""Coverage tests for app.domain.evaluation uncovered paths."""

from __future__ import annotations


class TestEvalDataset:
    def test_get_eval_dataset_returns_all_when_no_filters(self):
        from app.domain.evaluation import get_eval_dataset

        dataset = get_eval_dataset()
        assert len(dataset) > 0

    def test_get_eval_dataset_filters_by_category(self):
        from app.domain.evaluation import QuestionCategory, get_eval_dataset

        dataset = get_eval_dataset(category=QuestionCategory.COVERAGE)
        assert all(s.category == QuestionCategory.COVERAGE for s in dataset)

    def test_get_eval_dataset_filters_by_difficulty(self):
        from app.domain.evaluation import Difficulty, get_eval_dataset

        dataset = get_eval_dataset(difficulty=Difficulty.EASY)
        assert all(s.difficulty == Difficulty.EASY for s in dataset)
