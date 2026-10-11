"""Procedures domain adapter: playbook ingestion, retrieval, and the tool.

The procedures index is a second document index next to the policy one. It holds
domain procedure playbooks — the coverage determination checklist and the intake
triage procedure — so compliance can change a procedure by editing a markdown
file in ``app/data/procedures/`` instead of editing the system prompt.

"Skills" in this package means DOMAIN PROCEDURE PLAYBOOKS retrieved as documents
through that second index. Two other readings were rejected, and the reasoning is
recorded here so it is not re-litigated:

- Claude-style on-disk skill bundles (``SKILL.md`` plus scripts) have no loader
  in ADK 1.3, so there is nothing to register them with.
- The OpenAI Skills API is not consumable here: the app drives models through
  ``LiteLlm`` on ADK's chat-completions path rather than the Responses API, so a
  ``skill_reference`` cannot be attached to a request.

The chosen reading also matters for cost. The system prompt is already dense and
every added rule is paid for on every turn, so the playbook bodies stay out of
the prompt and are loaded on demand by ``get_procedure``.
"""

from __future__ import annotations

from app.domain.procedures.ingestion import (
    PROCEDURES_INGESTION_ENV,
    ingest_procedures,
    procedures_ingestion_enabled,
)
from app.domain.procedures.retrieval import (
    PROCEDURE_RETRIEVER_SPEC,
    ProcedureRetriever,
    retrieve_procedures,
)
from app.domain.procedures.store import ProcedureVersionStore, get_active_version_record
from app.domain.procedures.tools import get_procedure

__all__ = [
    "PROCEDURES_INGESTION_ENV",
    "PROCEDURE_RETRIEVER_SPEC",
    "ProcedureRetriever",
    "ProcedureVersionStore",
    "get_active_version_record",
    "get_procedure",
    "ingest_procedures",
    "procedures_ingestion_enabled",
    "retrieve_procedures",
]
