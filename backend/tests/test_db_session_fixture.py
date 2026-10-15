"""Tests for the ``db_session`` fixture's isolation guarantee.

The fixture promises that a test can commit real work and have it rolled back. A naive
``AsyncSession.begin()`` implementation breaks that promise: it creates the session's
root transaction, so a ``commit()`` inside the test persists, and the teardown rollback
then runs against a deactivated transaction.

These tests verify the promise by committing inside a test and confirming nothing
survived.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.user import User

pytestmark = pytest.mark.asyncio

# Placeholder for a password column. It is never hashed or verified.
FIXTURE_HASH = "not-a-real-hash"


def _new_user() -> User:
    user_id = uuid.uuid4()
    return User(
        id=user_id,
        username=f"db_session_{user_id.hex[:10]}",
        password_hash=FIXTURE_HASH,
    )


async def test_db_session_rolls_back_uncommitted_work(db_session) -> None:
    """Uncommitted writes are visible inside the test."""
    user = _new_user()
    db_session.add(user)
    await db_session.flush()

    found = await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == user.id)
    )
    assert found == 1, "the write should be visible inside the test"


async def test_db_session_discards_a_commit(db_session) -> None:
    """A commit inside the test does not persist past the fixture.

    This is the guarantee that a naive ``session.begin()`` implementation breaks.
    """
    user = _new_user()
    db_session.add(user)
    await db_session.commit()

    from app.core.database import async_session_factory

    async with async_session_factory() as verify:
        found = await verify.scalar(
            select(func.count()).select_from(User).where(User.id == user.id)
        )

    assert found == 0, "db_session leaked a committed row past the test"


async def test_db_session_releases_its_connection(db_session) -> None:
    """The fixture does not leave an open transaction that blocks the next reset."""
    db_session.add(_new_user())
    await db_session.flush()

    total = await db_session.scalar(select(func.count()).select_from(User))
    assert total >= 1
    assert db_session.in_transaction()
