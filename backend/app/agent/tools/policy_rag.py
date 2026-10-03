"""
Deprecated re-export shim for the policy RAG tool.

The ``query_policy`` tool moved to
:mod:`app.domain.policies.tools` (ragkit
plan 06). This module keeps the historical
import path working until plan 07 removes
the shims.

.. deprecated::
    Use :mod:`app.domain.policies.tools` instead.
"""

from app.domain.policies.tools import query_policy

__all__ = ["query_policy"]
