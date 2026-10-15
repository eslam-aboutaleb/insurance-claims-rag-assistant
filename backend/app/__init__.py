"""
OmniCare Financial Backend Application Package.

This is the root package for the FastAPI backend service. It exposes the
centralized application settings and serves as the namespace root for all
backend sub-packages:

  - ``app.agent``        -- Google ADK agent definition, tools, and prompts.
  - ``app.api``          -- REST API endpoint routers.
  - ``app.audit``        -- the append-only audit trail.
  - ``app.core``         -- infrastructure: settings, database, Redis clients,
    idempotency, rate limiting, transport middleware.
  - ``app.domain``       -- business logic per bounded context (claims,
    policies, procedures, notifications, evaluation).
  - ``app.models``       -- SQLAlchemy ORM models, grouped by bounded context.
  - ``app.observability``-- logging, metrics, tracing, and LLM instrumentation.
  - ``app.schemas``      -- Pydantic request/response models.
  - ``app.security``     -- authentication and authorization.
  - ``app.workers``      -- outbox drain workers.
"""

from app.core.config import settings

__all__ = ["settings"]
