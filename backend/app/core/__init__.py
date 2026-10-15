"""
Infrastructure and platform plumbing for the OmniCare backend.

This package holds everything that is not domain logic and not transport:

  - ``config`` -- the validated settings singleton every module reads.
  - ``database`` -- the async engine, session factory, and the ``get_db``
    FastAPI dependency.
  - ``redis_client`` -- the RESP wire-protocol clients shared by the
    rate-limit storage and the idempotency cache, so the protocol is
    implemented exactly once.
  - ``idempotency`` -- replay protection for the chat endpoint.
  - ``rate_limiter`` -- the SlowAPI limiter and its Redis-backed counters.
  - ``middleware`` -- transport-level middleware (request size limits).

Nothing in this package may import from ``app.domain``, ``app.api``, or
``app.agent``; it is the bottom layer the rest of the application sits on.

Modules import the submodules they need directly (``from app.core.database
import get_db``). This package deliberately re-exports nothing: importing
``app.core.config`` must stay as cheap and side-effect-free as the old
``app.config`` import was, so the engine is built only when a module asks
``app.core.database`` for it.
"""
