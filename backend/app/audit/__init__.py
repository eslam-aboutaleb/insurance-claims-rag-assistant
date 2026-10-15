"""
The audit trail: one append-only row per high-value action.

``record_audit`` is what a route calls directly, inside the transaction that
performs the audited action, so the entry commits with it. The claim-lifecycle
half of the trail is recorded from a ``ClaimEvent`` flush listener that is
installed when this package is imported.

The implementation lives in ``app.audit.service``; re-exporting ``record_audit``
here keeps ``from app.audit import record_audit`` as the stable import path.
"""

from app.audit.service import record_audit

__all__ = ["record_audit"]
