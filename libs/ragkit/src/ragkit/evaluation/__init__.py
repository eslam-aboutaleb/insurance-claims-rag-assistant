"""RAG evaluation subsystem.

Retrieval metrics, answer-quality judges, the
evaluation runner, and the evaluation dataset
types. All host-specific dependencies (retriever,
dataset, settings) are injected by the caller.
"""

from ragkit.evaluation.answer import (
    AnswerMetrics,
    AnswerScore,
    evaluate_answer_heuristic,
    evaluate_answer_llm,
)
from ragkit.evaluation.dataset import (
    Difficulty,
    EvalSample,
    QuestionCategory,
)
from ragkit.evaluation.retrieval import (
    LabeledQuery,
    RagEvaluationHarness,
    RetrievalMetrics,
)
from ragkit.evaluation.runner import (
    EvalConfig,
    EvalResult,
    RetrievalFunction,
    run_evaluation,
)

__all__ = [
    "AnswerMetrics",
    "AnswerScore",
    "Difficulty",
    "EvalConfig",
    "EvalResult",
    "EvalSample",
    "LabeledQuery",
    "QuestionCategory",
    "RetrievalFunction",
    "RetrievalMetrics",
    "RagEvaluationHarness",
    "evaluate_answer_heuristic",
    "evaluate_answer_llm",
    "run_evaluation",
]
