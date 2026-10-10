"""
Pydantic schemas for the OmniCare Financial backend.

This module defines the request/response envelopes, validation models,
and error payloads used across all API v1 endpoints. Keeping all schemas
in a single module ensures:

  - Consistent validation rules (e.g., claim amounts must be positive).
  - A single source of truth for the OpenAPI contract exposed at /docs.
  - Reusable models between the REST layer and the agent tool layer.

Security-sensitive schemas (``ClaimSubmission``) enforce strict field
constraints to prevent malformed or malicious payloads from reaching
the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Chat Schemas --------------------------------------------------------


class ChatRequest(BaseModel):
    """Request schema for the ``POST /api/v1/chat`` endpoint.

    Attributes:
        message: The user's incoming chat message or insurance inquiry.
            Limited to 4000 characters to prevent abuse and control token
            costs for LLM processing.
    """

    message: str = Field(
        ...,
        description="User's incoming chat message or insurance inquiry",
        examples=["What is covered under water damage?"],
        max_length=4000,
    )

    model_config = ConfigDict(
        str_strip_whitespace=False,
        json_schema_extra={
            "example": {
                "message": "What is covered under water damage?",
            }
        },
    )


class ToolCallInfo(BaseModel):
    """Schema for tool call information captured during agent reasoning.

    Attributes:
        name: The name of the tool invoked by the agent.
        arguments: The arguments passed to the tool as a dict.
        result: The result returned by the tool, or None if not yet recorded.
    """

    name: str = Field(..., description="Name of the tool called")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Arguments passed to the tool"
    )
    result: dict[str, Any] | None = Field(default=None, description="Result returned by the tool")


class ChatResponse(BaseModel):
    """Response schema for the ``POST /api/v1/chat`` endpoint.

    Attributes:
        response: The agent's synthesized text response.
        sources: Document citation sources and policy sections referenced
            in the response.
        tool_calls: List of tools invoked with arguments and execution results,
            providing an auditable trace of the agent's reasoning path.
    """

    response: str = Field(..., description="Agent's synthesized text response")
    sources: list[str] = Field(
        default_factory=list,
        description="Document citation sources and policy sections referenced",
    )
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of tools invoked with arguments and execution results",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "response": "Water damage caused by sudden pipe bursts is covered up to $25,000.",
                "sources": ["Section 1: Home Water Damage Coverage (sample_policy.md)"],
                "tool_calls": [
                    {
                        "name": "query_policy",
                        "arguments": {"query": "water damage coverage"},
                        "result": {"chunks_found": 1},
                    }
                ],
            }
        }
    )


# --- Health Schema -------------------------------------------------------


class HealthResponse(BaseModel):
    """Response schema for the ``GET /api/v1/health`` endpoint.

    Attributes:
        status: Service health status. Either ``"healthy"`` or ``"unhealthy"``.
    """

    status: str = Field(
        default="healthy",
        description="Service health status ('healthy' or 'unhealthy')",
    )


# --- Error Envelope ------------------------------------------------------


class ErrorDetail(BaseModel):
    """Structured error payload adhering to standardized REST error practices.

    Attributes:
        code: Machine-readable error category code (e.g., ``VALIDATION_ERROR``).
        message: Human-readable explanation of the error suitable for end users.
        details: Additional context or validation failure breakdown, or None.
    """

    code: str = Field(..., description="Machine-readable error category code")
    message: str = Field(..., description="Human-readable explanation of the error")
    details: Any | None = Field(
        default=None, description="Additional context or validation failure breakdown"
    )


class ErrorResponse(BaseModel):
    """Standardized top-level API error envelope.

    All error responses from the API follow this shape:
    ``{"error": {"code": "...", "message": "...", "details": ...}}``
    """

    error: ErrorDetail = Field(..., description="Error detail container")


# --- Claim Submission Schema ---------------------------------------------


class ClaimSubmission(BaseModel):
    """Pydantic model for validating new claim submissions.

    All fields are required and validated before any database write occurs.
    This prevents malformed or malicious payloads from reaching the
    persistence layer.

    Attributes:
        policy_number: The policyholder's policy number.
        claim_type: Category/type of insurance claim.
        amount: Total claim amount in US dollars. Must be greater than 0.
        description: Detailed factual description of the incident or claim
            event. Must be at least 10 characters to ensure sufficient
            detail for claims processing.
    """

    policy_number: str = Field(
        ...,
        description="Policy number associated with the policyholder (e.g., POL-1092)",
        min_length=1,
    )
    claim_type: str = Field(
        ...,
        description="Category/type of insurance claim (e.g., Water Damage, Personal Property)",
        min_length=1,
    )
    amount: Decimal = Field(
        ...,
        description="Total claim amount in US dollars (must be greater than 0)",
        gt=Decimal("0"),
    )
    description: str = Field(
        ...,
        description="Detailed factual description of the incident or claim event",
        min_length=10,
    )

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "policy_number": "POL-1092",
                "claim_type": "Water Damage",
                "amount": 2500.0,
                "description": "Sudden frozen pipe burst in master bathroom causing floor flooding",
            }
        },
    )


# --- Claim Submission Confirmation Schema --------------------------------


class ClaimSubmissionPrepareRequest(BaseModel):
    """Request schema for preparing a claim submission (before confirmation).

    Attributes:
        policy_number: The policyholder's policy number.
        claim_type: Category/type of insurance claim.
        amount: Total claim amount in US dollars.
        description: Detailed factual description of the incident.
    """

    policy_number: str = Field(..., min_length=1)
    claim_type: str = Field(..., min_length=1)
    amount: Decimal = Field(..., gt=Decimal("0"))
    description: str = Field(..., min_length=10)


class ClaimSubmissionPrepareResponse(BaseModel):
    """Response schema for preparing a claim submission.

    Attributes:
        confirmation_token: UUID token the frontend must present to confirm.
        expires_at: ISO timestamp when the token expires.
        status: Current status of the pending submission.
    """

    confirmation_token: str = Field(..., description="Token to present at /confirm endpoint")
    expires_at: datetime = Field(..., description="When this pending submission expires")
    status: str = Field(default="pending", description="Current submission status")


class ClaimConfirmationRequest(BaseModel):
    """Request schema for confirming a claim submission.

    Attributes:
        confirmation_token: The token returned by the prepare endpoint.
    """

    confirmation_token: str = Field(
        ...,
        description="Confirmation token from prepare endpoint",
        min_length=1,
    )


class ClaimConfirmationResponse(BaseModel):
    """Response schema for a confirmed claim submission.

    Attributes:
        success: Whether the confirmation succeeded.
        claim_id: The generated claim ID if successful.
        status: Current claim status.
        message: Human-readable confirmation message.
    """

    success: bool = Field(..., description="Whether the claim was submitted successfully")
    claim_id: str | None = Field(default=None, description="Generated claim ID on success")
    status: str | None = Field(default=None, description="Current claim status")
    message: str = Field(..., description="Human-readable result message")


# --- Authentication Schemas ----------------------------------------------


class UserSignup(BaseModel):
    """Request schema for user registration.

    Attributes:
        username: Desired username. Must be at least 3 characters.
        password: Account password. Must be at least 6 characters.
    """

    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


class UserSignin(BaseModel):
    """Request schema for user authentication.

    Attributes:
        username: The user's registered username.
        password: The user's plaintext password for verification.
    """

    username: str
    password: str


class Token(BaseModel):
    """Response schema for authentication endpoints (signup/signin).

    Attributes:
        access_token: The signed JWT access token.
        token_type: The token type (always ``"bearer"``).
        user_id: The authenticated user's UUID as a string.
    """

    access_token: str
    token_type: str
    user_id: str


# --- Conversation Schemas ------------------------------------------------


class ConversationMetadata(BaseModel):
    """Lightweight metadata for a conversation, used in sidebar lists.

    Attributes:
        id: The conversation's primary key UUID.
        title: Human-readable conversation title.
        created_at: Timestamp of conversation creation.
        updated_at: Timestamp of the last message or metadata update.
    """

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    """Response schema for listing a user's conversations.

    Attributes:
        conversations: Ordered list of conversation metadata objects.
    """

    conversations: list[ConversationMetadata]


class ConversationDetailResponse(ConversationMetadata):
    """Response schema for fetching a single conversation with full message history.

    Attributes:
        messages: Full list of message objects from the conversation history.
            Each message contains at least ``id``, ``role``, ``content``, and
            ``timestamp``, plus optional ``sources`` and ``tool_calls`` fields.
    """

    messages: list[dict[str, Any]]


class UserMe(BaseModel):
    """Response schema for the ``GET /api/v1/auth/me`` endpoint.

    Attributes:
        user_id: The authenticated user's UUID as a string.
    """

    user_id: str = Field(..., description="Authenticated user's UUID")


# Rebuild models that reference uuid to resolve forward references.
ConversationListResponse.model_rebuild()
ConversationDetailResponse.model_rebuild()


# --- RAG Evaluation Schemas ---------------------------------------------


class RagEvalRequest(BaseModel):
    """Request schema for initiating a RAG evaluation run.

    Attributes:
        mode: Evaluation mode. 'retrieval-only' evaluates retrieval
            quality only; 'context' scores the retrieved context
            against the gold answer; 'end-to-end' is currently
            mapped to 'context' until an answer generator is wired.
        judge: Evaluation judge strategy ('heuristic' or 'llm').
        n_results: Maximum candidates to retrieve per query.
        category: Optional category filter (coverage, limits, exclusions, etc.).
        difficulty: Optional difficulty filter (easy, medium, hard).
    """

    mode: Literal["retrieval-only", "context", "end-to-end"] = Field(
        default="end-to-end",
        description=(
            "Evaluation mode: 'retrieval-only', 'context' (scores the "
            "retrieved context against the gold answer), or 'end-to-end' "
            "(currently mapped to 'context' until an answer generator is wired)"
        ),
    )
    judge: Literal["heuristic", "llm"] = Field(
        default="heuristic",
        description="Judge strategy: 'heuristic' or 'llm'",
    )
    n_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of candidates to retrieve per query",
    )
    category: str | None = Field(
        default=None,
        description="Optional filter by question category",
    )
    difficulty: str | None = Field(
        default=None,
        description="Optional filter by query difficulty",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "mode": "end-to-end",
                "judge": "heuristic",
                "n_results": 5,
                "category": None,
                "difficulty": None,
            }
        }
    )


class RagEvalResponse(BaseModel):
    """Response schema containing RAG evaluation metrics and results.

    Attributes:
        timestamp: ISO timestamp when evaluation completed.
        config: Configuration dictionary used for the evaluation run.
        retrieval_metrics: Aggregated retrieval metrics (recall, precision, MRR, latency).
        answer_metrics: Aggregated answer quality metrics (faithfulness, relevance, etc.).
        per_sample_results: Granular per-sample evaluation breakdown.
        summary: Human-readable evaluation summary report.
        duration_seconds: Total execution duration in seconds.
    """

    timestamp: str = Field(..., description="ISO 8601 timestamp of evaluation completion")
    config: dict[str, Any] = Field(..., description="Configuration used for the run")
    retrieval_metrics: dict[str, Any] = Field(
        default_factory=dict,
        description="Aggregated retrieval quality metrics",
    )
    answer_metrics: dict[str, Any] = Field(
        default_factory=dict,
        description="Aggregated answer quality metrics",
    )
    per_sample_results: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-sample evaluation results",
    )
    summary: str = Field(..., description="Formatted summary report")
    duration_seconds: float = Field(..., description="Total execution duration in seconds")
