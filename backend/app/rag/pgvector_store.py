"""
Re-export shim for the ragkit pgvector store.

The implementation moved to ``ragkit.stores.pgvector`` (ragkit
extraction plan 03). This module keeps the historical import path
working until plan 07 removes the shims, so ``ingest.py``,
``claims_rag.py``, and ``embedding_jobs.py`` keep importing the
validation helpers from here in the meantime.

Security model (unchanged, now enforced by ragkit):
  - All user-supplied **values** are passed as bound parameters via
    SQLAlchemy's parameterized queries. They never appear in the SQL
    string, so they cannot inject SQL.
  - All **SQL identifiers** (table names, column names, filter keys)
    are validated against a strict allowlist regex
    (``^[A-Za-z_][A-Za-z0-9_]*$``) before interpolation into raw SQL
    strings.
"""

from ragkit.stores.pgvector import PgVectorStore
from ragkit.validation import validate_embedding, validate_identifier

# Private aliases kept for the host modules that still import the
# historical private names (ingest.py, claims_rag.py, embedding_jobs.py).
# Plans 04-06 rewire those imports onto the public names; plan 07
# removes the aliases with the rest of the shim.
_validate_identifier = validate_identifier
_validate_embedding = validate_embedding

__all__ = [
    "PgVectorStore",
    "validate_identifier",
    "validate_embedding",
    "_validate_identifier",
    "_validate_embedding",
]
