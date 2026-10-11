"""
API v1 Router - mounts all v1 endpoint routers under the /api/v1 prefix.

This router acts as the single aggregation point for all version-1 API
endpoints. Each sub-router is responsible for its own domain:

  - ``/health``        -- Service health and dependency checks.
  - ``/chat``          -- Synchronous chat endpoint with idempotency.
  - ``/chat/stream``   -- Streaming SSE chat endpoint.
  - ``/chat/conversations`` -- Conversation history CRUD operations.
  - ``/claims``        -- Claim lifecycle: prepare, confirm, cancel, list,
                          read, status transition, withdraw.
  - ``/feedback``      -- Answer quality ratings on the caller's own answers.
  - ``/notifications``-- In-app notification feed for claim status changes.
  - ``/admin``         -- Administrative queue, audit and feedback views.
  - ``/rag``           -- RAG evaluation and dataset endpoints.
  - ``/auth``          -- Public authentication endpoints (signup/signin/logout).

Including this router in ``app/main.py`` makes all v1 endpoints available
at their documented paths without requiring individual route registrations.
The ``/admin`` routes are role-gated in their own module; they are mounted here
so they share the versioned prefix, not to make them public.

``GET /api/v1/health/detailed`` is not routed through here: it is registered
straight on the app from ``app.observability.routes`` so the pinned
``/health`` contract and its response shape stay untouched.
"""

from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.chat_stream import router as chat_stream_router
from app.api.v1.claims import router as claims_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.feedback import router as feedback_router
from app.api.v1.health import router as health_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.rag_eval import router as rag_eval_router

router = APIRouter(prefix="/api/v1")

router.include_router(health_router, tags=["Health"])
router.include_router(chat_router, tags=["Chat"])
router.include_router(conversations_router, tags=["Conversations"])
router.include_router(chat_stream_router, tags=["Chat"])
router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
router.include_router(claims_router, prefix="/claims", tags=["Claims"])
router.include_router(feedback_router, prefix="/feedback", tags=["Feedback"])
router.include_router(notifications_router, prefix="/notifications", tags=["Notifications"])
router.include_router(admin_router, tags=["Administration"])
router.include_router(rag_eval_router, tags=["RAG Evaluation"])
