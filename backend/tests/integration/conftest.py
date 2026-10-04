"""Shared fixtures for Tier B integration tests.

Tier B exercises the real PostgreSQL container -- pgvector distance search,
tsvector full-text search, and the RRF merge -- with a deterministic hashing
embedder standing in for the LLM embedding provider. The double is installed
per test by an autouse fixture so it cannot leak into the unit or contract
suites that rely on the zero-vector double from the parent conftest.

The whole tier is skipped unless ``OMNICARE_TEST_DATABASE_URL`` names a
dedicated test database: these tests seed and delete real rows.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

import litellm
import pytest
import pytest_asyncio
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
EMBEDDING_DIM = 1536


def _test_database_url_configured() -> bool:
    """Return True when OMNICARE_TEST_DATABASE_URL is set in the env or .env."""
    if os.environ.get("OMNICARE_TEST_DATABASE_URL"):
        return True
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return False
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("OMNICARE_TEST_DATABASE_URL="):
            return True
    return False


if not _test_database_url_configured():
    pytest.skip(
        "OMNICARE_TEST_DATABASE_URL is not set; skipping Tier B integration tests",
        allow_module_level=True,
    )


def hash_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Return a deterministic hashing embedding for ``text``.

    Each token is hashed into one signed dimension of a ``dim``-dimensional
    vector, which is then L2-normalised. Two texts that share tokens get
    close vectors; disjoint texts land at distance sqrt(2) -- beyond the
    default RAG distance threshold of 1.3 -- so vector search behaves like
    bag-of-words similarity without any network calls.
    """
    vector = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dim
        vector[index] += 1.0 if digest[2] % 2 == 0 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0.0:
        vector = [value / norm for value in vector]
    return vector


class _HashingEmbeddingResponse:
    """Mimics the LiteLLM embedding response shape (``response.data``)."""

    def __init__(self, inputs: list[str]):
        self.data = [{"embedding": hash_embedding(item)} for item in inputs]


def _hashing_embedding(**kwargs: Any) -> _HashingEmbeddingResponse:
    inputs = kwargs.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    return _HashingEmbeddingResponse(list(inputs))


@pytest.fixture(autouse=True)
def _tier_b_embedding_double(monkeypatch):
    """Install the deterministic embedder for this Tier B test only."""
    monkeypatch.setattr(litellm, "embedding", _hashing_embedding)
    yield


# --- Seed data -----------------------------------------------------------

USER_A_USERNAME = "tierb-user-a"
USER_B_USERNAME = "tierb-user-b"

CLAIMS_SEED: list[dict[str, Any]] = [
    {
        "claim_id": "CLM-8821",
        "policy_number": "POL-1092",
        "claim_type": "Water Damage",
        "status": "Approved",
        "amount": 3500.00,
        "description": "Pipe burst causing kitchen flooding and water damage.",
        "owner": "a",
    },
    {
        "claim_id": "CLM-9014",
        "policy_number": "POL-3341",
        "claim_type": "Personal Property",
        "status": "Under Review",
        "amount": 1200.00,
        "description": "Burglary resulting in stolen electronics and furniture.",
        "owner": "a",
    },
    {
        "claim_id": "CLM-7700",
        "policy_number": "POL-5588",
        "claim_type": "Fire Damage",
        "status": "Submitted",
        "amount": 8900.00,
        "description": "Fire damage to apartment ceiling and walls.",
        "owner": "b",
    },
]


@pytest_asyncio.fixture(scope="function")
async def seeded_users() -> dict[str, uuid.UUID]:
    """Create the two Tier B users and return their UUIDs keyed a/b."""
    from app.database import async_session_factory
    from app.models.user import User

    users = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    async with async_session_factory() as session:
        session.add_all(
            [
                User(
                    id=users["a"],
                    username=USER_A_USERNAME,
                    password_hash="tierb-placeholder-hash",  # noqa: S106 - fixture row, never verified
                ),
                User(
                    id=users["b"],
                    username=USER_B_USERNAME,
                    password_hash="tierb-placeholder-hash",  # noqa: S106 - fixture row, never verified
                ),
            ]
        )
        await session.commit()
    return users


@pytest_asyncio.fixture(scope="function")
async def seeded_policy() -> int:
    """Ingest the sample policy through the real ingestion path."""
    from app.domain.policies.ingestion import ingest_policy

    return await ingest_policy()


@pytest_asyncio.fixture(scope="function")
async def seeded_claims(
    seeded_users: dict[str, uuid.UUID],
) -> list[dict[str, Any]]:
    """Index the three seed claims into the claims vector store."""
    from app.domain.claims.ingest import ingest_claim

    seeded: list[dict[str, Any]] = []
    for claim in CLAIMS_SEED:
        claim_uuid = uuid.uuid4()
        await ingest_claim(
            id=claim_uuid,
            claim_id=claim["claim_id"],
            owner_id=seeded_users[claim["owner"]],
            claim_type=claim["claim_type"],
            description=claim["description"],
            policy_number=claim["policy_number"],
            status=claim["status"],
            amount=claim["amount"],
        )
        seeded.append(
            {
                **claim,
                "id": claim_uuid,
                "owner_id": seeded_users[claim["owner"]],
            }
        )
    return seeded


@pytest_asyncio.fixture(scope="function")
async def tierb_seed(
    seeded_users: dict[str, uuid.UUID],
    seeded_policy: int,
    seeded_claims: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine every Tier B seed into one namespace."""
    return {
        "users": seeded_users,
        "policy_chunks": seeded_policy,
        "claims": seeded_claims,
    }


async def section_chunk_ids() -> dict[str, list[str]]:
    """Return the chunk ids currently stored, keyed by section title."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        rows = (await session.execute(text("SELECT section, chunk_id FROM policy_chunks"))).all()
    mapping: dict[str, list[str]] = {}
    for section, chunk_id in rows:
        mapping.setdefault(section, []).append(chunk_id)
    return mapping
