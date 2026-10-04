"""
RAG Evaluation API endpoints.

POST /api/v1/rag/evaluate - Trigger an evaluation run (retrieval-only or end-to-end)
GET  /api/v1/rag/dataset  - List the curated evaluation queries and ground truth
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import get_current_user
from app.domain.evaluation import (
    Difficulty,
    EvalConfig,
    QuestionCategory,
    get_eval_dataset,
    run_evaluation,
)
from app.rate_limiter import limiter
from app.schemas.models import ErrorResponse, RagEvalRequest, RagEvalResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/rag/evaluate",
    response_model=RagEvalResponse,
    status_code=status.HTTP_200_OK,
    summary="Run RAG Evaluation",
    description=(
        "Executes RAG evaluation across the curated test dataset. "
        "Supports retrieval quality metrics (Recall@K, Precision@K, MRR, latency) "
        "and answer quality metrics (faithfulness, relevance, completeness, conciseness)."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Evaluation executed successfully with metrics.",
            "model": RagEvalResponse,
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Internal error during evaluation execution.",
            "model": ErrorResponse,
        },
    },
)
@limiter.limit("5/minute")
async def evaluate_rag(
    payload: RagEvalRequest,
    request: Request,
    current_user_id: str = Depends(get_current_user),
) -> RagEvalResponse:
    """Run RAG evaluation pipeline and return structured metrics."""
    try:
        category_enum = None
        if payload.category:
            try:
                category_enum = QuestionCategory(payload.category.lower())
            except ValueError:
                valid = [c.value for c in QuestionCategory]
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid category '{payload.category}'. Valid: {valid}",
                ) from None

        difficulty_enum = None
        if payload.difficulty:
            try:
                difficulty_enum = Difficulty(payload.difficulty.lower())
            except ValueError:
                valid = [d.value for d in Difficulty]
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid difficulty '{payload.difficulty}'. Valid: {valid}",
                ) from None

        config = EvalConfig(
            mode=payload.mode,
            judge=payload.judge,
            n_results=payload.n_results,
            category=category_enum,
            difficulty=difficulty_enum,
        )

        result = await run_evaluation(config)

        return RagEvalResponse(
            timestamp=result.timestamp,
            config=result.config,
            retrieval_metrics=result.retrieval_metrics,
            answer_metrics=result.answer_metrics,
            per_sample_results=result.per_sample_results,
            summary=result.summary,
            duration_seconds=result.duration_seconds,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("RAG evaluation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"RAG evaluation failed: {exc}",
        ) from exc


@router.get(
    "/rag/dataset",
    status_code=status.HTTP_200_OK,
    summary="List RAG Evaluation Dataset",
    description=(
        "Returns the curated evaluation dataset with queries, expected sections, and gold answers."
    ),
)
async def list_eval_dataset(
    category: str | None = None,
    difficulty: str | None = None,
    current_user_id: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Retrieve the curated evaluation dataset with optional filtering."""
    category_enum = None
    if category:
        try:
            category_enum = QuestionCategory(category.lower())
        except ValueError:
            valid = [c.value for c in QuestionCategory]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid category '{category}'. Valid: {valid}",
            ) from None

    difficulty_enum = None
    if difficulty:
        try:
            difficulty_enum = Difficulty(difficulty.lower())
        except ValueError:
            valid = [d.value for d in Difficulty]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid difficulty '{difficulty}'. Valid: {valid}",
            ) from None

    dataset = get_eval_dataset(category=category_enum, difficulty=difficulty_enum)
    return [
        {
            "query": s.query,
            "expected_sections": s.expected_sections,
            "gold_answer": s.gold_answer,
            "category": s.category.value,
            "difficulty": s.difficulty.value,
            "metadata": s.metadata,
        }
        for s in dataset
    ]
