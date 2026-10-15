"""
Context variables for propagating request-scoped state through the OmniCare
backend without explicit parameter threading.

This module defines ``current_user_id``, a ``ContextVar`` that holds the UUID
of the authenticated user for the duration of an async request.

Why ContextVars instead of global state?
  - FastAPI runs many requests concurrently on a single event loop. A global
    variable would be shared across requests, causing data races where one
    user's ID leaks into another user's agent tool execution.
  - ``ContextVar`` is designed for exactly this problem: it provides
    request-local storage that flows through ``async/await`` boundaries and
    is isolated per concurrent task.

Usage pattern:
  - ``get_current_user`` (in ``app/security/auth.py``) sets ``current_user_id``
    after successful JWT validation.
  - Agent tools and other downstream code read ``current_user_id.get()`` to
    scope database queries and audit log entries to the correct user.
"""

import contextvars
import uuid

# Context variable holding the authenticated user's UUID for the current
# async request context. This is set by ``get_current_user`` and read by
# agent tools and business logic to enforce user-scoped access control.
current_user_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("current_user_id")
