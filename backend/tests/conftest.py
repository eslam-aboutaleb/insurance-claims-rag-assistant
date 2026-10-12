"""
Pytest configuration and shared fixtures for the OmniCare Financial backend test suite.

Provides fixtures for:
  - test_client: FastAPI TestClient instance configured for testing
  - sample_claims_path: Isolated temporary copy of mock_claims.json
  - mock_current_user: Overrides the get_current_user dependency with a fixed test user ID
  - db_session: AsyncSession wrapped in an outer transaction that is rolled back
  - _clean_database_state: autouse per-test deletion of every row in the schema,
    run on BOTH setup and teardown
  - _clean_database_once: the same reset, run once before the session starts

The database target is resolved from OMNICARE_TEST_DATABASE_URL, then the project
``.env``, and is refused unless the database name contains ``test``: the reset
deletes every row in every application table, so pointing the suite at the
database the running stack uses would destroy live development data.
"""

import asyncio
import json
import os
import shutil
import sys
import time
import warnings
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError


def _in_docker() -> bool:
    try:
        return Path("/.dockerenv").exists() or (
            Path("/proc/self/cgroup").exists()
            and "docker" in Path("/proc/self/cgroup").read_text(errors="ignore")
        )
    except Exception:
        return False


def _read_dotenv(key: str) -> str | None:
    """Return a key's value from the project ``.env`` file, or None if absent."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(f"{key}="):
            return line.partition("=")[2].strip().replace("@localhost:", "@127.0.0.1:")
    return None


def _resolve_test_database_url() -> str:
    """Pick one unambiguous PostgreSQL target for the test session.

    Priority:

    1. ``OMNICARE_TEST_DATABASE_URL`` from the process environment, then from ``.env``.
    2. The project's ``.env`` ``DATABASE_URL``.
    3. A local default.

    Preference for a *test* database matters: the per-test fixture truncates every
    application table, so pointing the suite at the database the running stack uses
    would destroy live development data. ``_assert_database_is_dedicated`` enforces that
    the target really is disposable.

    The host is never ``localhost``: this machine commonly runs a Homebrew PostgreSQL on
    127.0.0.1 and [::1] *and* a database container published on the wildcard address for
    the same port. ``localhost`` lets the resolver pick either, so a signup and the reset
    preceding it could land on different servers and one deletes the other's rows.
    """
    override = os.environ.get("OMNICARE_TEST_DATABASE_URL")
    if override:
        return override

    from_file = _read_dotenv("OMNICARE_TEST_DATABASE_URL")
    if from_file:
        return from_file

    app_url = _read_dotenv("DATABASE_URL")
    if app_url:
        return app_url

    return f"postgresql+asyncpg://omnicare:omnicare_password@{DB_HOST}:5432/omnicare"


# Each request runs on the ``TestClient`` portal loop while the database fixtures
# reset the schema on their own ``asyncio.run`` loop. NullPool removes the pooled
# connection hand-off between those loops; see app/database.py.
os.environ["OMNICARE_TEST_NULLPOOL"] = "1"
# The NullPool selection is gated on ENVIRONMENT=test so a stray value cannot turn it on
# in a deployment, which means the suite must declare that tier for itself.
os.environ["ENVIRONMENT"] = "test"

DB_HOST = "db" if _in_docker() else "127.0.0.1"
os.environ["DATABASE_URL"] = _resolve_test_database_url()

# The host must be explicit rather than the ``localhost`` hostname. This machine
# commonly runs two servers on port 5432 at once: a Homebrew Postgres bound to
# 127.0.0.1 and [::1], and the omnicare-db container published on the wildcard
# address. ``localhost`` lets the resolver pick either, so a signup and the reset
# that precedes it can land on different servers and one deletes the other's rows.
# Set OMNICARE_TEST_DATABASE_URL to point the suite at a specific server.

DATABASE_URL = os.environ["DATABASE_URL"]

_SERVER_IDENTITY: tuple[str, str] | None = None


async def _read_server_identity() -> str:
    """Return a string identifying the PostgreSQL server behind the engine."""
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as session:
        row = await session.execute(
            text(
                "SELECT current_database() || '@' || inet_server_addr()::text "
                "|| '/' || pg_postmaster_start_time()::text"
            )
        )
        return row.scalar_one()


def _assert_stable_server(identity: str) -> None:
    """Fail fast if the suite's connection is not landing on the same server twice.

    Two servers listening on the same port is invisible until rows written by a test
    vanish mid-test. Pinning the host handles the common case; this check catches a
    server swapping underneath a long run and names the cause instead of letting the
    suite fail with a confusing foreign-key error.

    Args:
        identity: The identity read on this pass, obtained from the caller's event loop.
    """
    global _SERVER_IDENTITY  # noqa: PLW0603

    if _SERVER_IDENTITY is None:
        _SERVER_IDENTITY = identity
        return

    if identity != _SERVER_IDENTITY:
        raise AssertionError(
            "The test suite changed PostgreSQL server mid-run.\n"
            f"  first: {_SERVER_IDENTITY}\n"
            f"  now:   {identity}\n"
            "Two servers are listening on the same port, so connections are being "
            "distributed between them. Set OMNICARE_TEST_DATABASE_URL to an explicit "
            "host, or stop the competing server."
        )


# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Patch ingest_policy to avoid network calls during test client startup.
# Import and keep the real function before patching so e2e tests can use it.
import app.main as _app_main_module  # noqa: E402, PLC0415
from app.config import settings  # noqa: E402

_real_ingest_policy = _app_main_module.ingest_policy
_ingest_policy_patcher = patch(
    "app.main.ingest_policy", new_callable=__import__("unittest.mock").mock.AsyncMock
)
_ingest_policy_patcher.start()


@pytest.fixture(scope="function")
def real_ingest_policy():
    """Stop the global ingest_policy mock and restore the real function for e2e tests."""
    _ingest_policy_patcher.stop()
    yield _real_ingest_policy
    _ingest_policy_patcher.start()


@pytest.fixture(scope="function")
def sample_claims_path(tmp_path, monkeypatch):
    """
    Fixture providing an isolated temporary copy of mock_claims.json.

    Copies the baseline mock_claims.json into a temporary directory, falling
    back to two hard-coded claims when the baseline is absent, and returns the
    path for a test to pass to whatever reads it.

    It deliberately does not patch a setting: claims live in PostgreSQL, and
    ``Settings`` carries no claims-file field, so there is nothing to redirect.
    The historical ``settings.claims_file_path`` patch this fixture used to
    attempt was already a silent no-op behind its ``hasattr`` guard; the copy
    is kept because a test that does read the file gets one it cannot mutate.
    """
    original_claims = BACKEND_DIR / "app" / "data" / "mock_claims.json"
    temp_claims = tmp_path / "mock_claims.json"

    if original_claims.exists():
        shutil.copy(original_claims, temp_claims)
    else:
        baseline = [
            {
                "claim_id": "CLM-8821",
                "policy_number": "POL-1092",
                "claim_type": "Water Damage",
                "status": "Approved",
                "amount": 3500.00,
                "description": "Pipe burst causing kitchen flooding and water damage.",
            },
            {
                "claim_id": "CLM-9014",
                "policy_number": "POL-3341",
                "claim_type": "Personal Property",
                "status": "Under Review",
                "amount": 1200.00,
                "description": "Burglary resulting in stolen electronics and furniture.",
            },
        ]
        temp_claims.write_text(json.dumps(baseline, indent=2), encoding="utf-8")

    if hasattr(settings, "claims_file_path"):
        monkeypatch.setattr(settings, "claims_file_path", str(temp_claims))

    return temp_claims


@pytest.fixture(scope="function")
def test_client(sample_claims_path):
    """
    FastAPI TestClient fixture configured with isolated claims data.
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.database import engine  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415

    with TestClient(app) as client:
        yield client

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(engine.dispose())
            else:
                pending = [
                    task
                    for task in asyncio.all_tasks(loop=loop)
                    if not task.done() and task != asyncio.current_task()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(engine.dispose())
    except Exception as exc:  # pragma: no cover - surfaced in test output
        print(f"[test_client] engine dispose failed: {exc!r}")
        raise

    import time

    time.sleep(0.1)


@pytest.fixture(scope="function", autouse=True)
def _reset_engine():
    yield
    try:
        from app.database import engine  # noqa: PLC0415

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(engine.dispose())
            else:
                pending = [
                    task
                    for task in asyncio.all_tasks(loop=loop)
                    if not task.done() and task != asyncio.current_task()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(engine.dispose())
    except Exception as exc:  # pragma: no cover - cleanup best-effort
        print(f"[_reset_engine] post-test cleanup failed: {exc!r}")


@pytest.fixture(scope="function", autouse=True)
def _clear_idempotency_cache():
    """Clear the idempotency cache between tests to ensure isolation."""
    from app.idempotency import clear_cache  # noqa: PLC0415

    yield
    clear_cache()


_TABLES_IN_DELETE_ORDER = (
    # Children first: every entry must precede the table its foreign key points
    # at, or the DELETE fails and the reset silently leaves rows behind.
    "answer_feedback",
    "audit_events",
    "claim_events",
    "procedure_chunks",
    "procedure_versions",
    "agent_session_events",
    "agent_sessions",
    "eval_runs",
    "conversation_messages",
    "policy_chunks",
    "embedding_jobs",
    "claims",
    "claim_submissions",
    "conversations",
    "policy_versions",
    "policy_ingestion_meta",
    "policies",
    "users",
)

_RESTART_SEQUENCES_SQL = """
DO $$
DECLARE
    seq record;
BEGIN
    FOR seq IN
        SELECT sequencename
        FROM pg_sequences
        WHERE schemaname = current_schema()
    LOOP
        EXECUTE format('ALTER SEQUENCE %I RESTART', seq.sequencename);
    END LOOP;
END $$;
"""


async def _delete_all_rows() -> None:
    """Delete every row from the application tables and restart sequences.

    Rows are removed with ``DELETE FROM`` in foreign-key-safe reverse dependency
    order rather than ``TRUNCATE ... CASCADE``. CASCADE would silently empty
    tables that other suites seed, which hides ordering bugs instead of
    surfacing them.

    Tables absent from the target database are skipped, so the reset works
    against any schema shape rather than only one that the application has
    already booted against.

    A bounded ``lock_timeout`` turns a blocked statement into a fast failure.
    The reset runs in its own transaction, so a timeout rolls back cleanly and
    leaves no partial state for the next attempt.
    """
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        present = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                )
            )
        }
        for table in _TABLES_IN_DELETE_ORDER:
            if table not in present:
                # Absent, not stale: the reset's contract is "these rows are
                # gone afterwards", which a table that does not exist already
                # satisfies. A test that genuinely needs it queries it and
                # fails loudly on its own, so this hides nothing.
                continue
            # Table names come from the fixed tuple above, never from test input.
            await session.execute(text(f'DELETE FROM "{table}"'))  # noqa: S608
        await session.execute(text(_RESTART_SEQUENCES_SQL))
        await session.commit()


_RESET_MARKER_TABLE = "users"


def _assert_database_is_dedicated(resolved_url: str) -> None:
    """Refuse to wipe a database that the developer's running stack is using.

    ``_clean_database_state`` issues ``DELETE FROM`` on every application table before
    each test. That is only safe against a database that exists purely for tests. Because
    the URL may come from the project ``.env`` -- the same file the Docker stack reads --
    an unguarded run would silently destroy live development data (users, conversations,
    claims, and the policy index).

    The guard is deliberately conservative: the database name must contain ``test``, or
    the developer must opt in explicitly with ``OMNICARE_TEST_ALLOW_DESTRUCTIVE_RESET=1``.
    """

    override = os.environ.get("OMNICARE_TEST_ALLOW_DESTRUCTIVE_RESET") == "1"
    database_name = resolved_url.rpartition("/")[2].split("?")[0]

    if override or "test" in database_name.lower():
        return

    raise RuntimeError(
        f"Refusing to run tests against '{database_name}': the per-test fixture deletes "
        "every row in every application table before each test.\n"
        "Point the suite at a dedicated database, for example:\n"
        "  OMNICARE_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:15432/omnicare_test\n"
        "or opt in explicitly if this database really is disposable:\n"
        "  OMNICARE_TEST_ALLOW_DESTRUCTIVE_RESET=1"
    )


def _reset_database(_attempts: int = 5) -> None:
    """Dispose the engine and clear all rows in an isolated event loop.

    The engine is a process-wide singleton whose pooled connections belong to
    whichever event loop last used them. Disposing before opening a fresh loop
    prevents ``Future attached to a different loop`` errors and additionally
    rolls back any transaction a test left open, which would otherwise block
    the ``DELETE`` statements.

    The application under test and the ``alembic`` subprocesses that
    ``test_client`` spawns share this database, so a reset can transiently lose
    a deadlock or lock race against them. Retrying with a short backoff settles
    that contention instead of failing a test that did nothing wrong.
    """
    from app.database import engine  # noqa: PLC0415

    async def _run() -> None:
        await engine.dispose()
        try:
            _assert_stable_server(await _read_server_identity())
            await _delete_all_rows()
        finally:
            await engine.dispose()

    # Pure string logic, so it runs before any loop is created.
    _assert_database_is_dedicated(DATABASE_URL)

    last_error: Exception | None = None
    for attempt in range(_attempts):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                asyncio.run(_run())
        except SQLAlchemyError as exc:
            last_error = exc
            if attempt == _attempts - 1:
                break
            time.sleep(0.2 * (attempt + 1))
        else:
            return

    raise AssertionError(
        f"Database reset failed after {_attempts} attempts: {last_error!r}"
    ) from last_error


@pytest.fixture(scope="session", autouse=True)
def _clean_database_once():
    """Clear the schema before the session starts.

    A developer's local database may hold rows from an interrupted run. Without this
    the very first test would start from that state.
    """
    _reset_database()


@pytest.fixture(scope="function", autouse=True)
def _clean_database_state():
    """Run every test against an empty schema, and leave one behind.

    Autouse and function-scoped. The reset runs on BOTH setup and teardown.

    Setup alone is not enough: a write that a test fires without awaiting -- a
    drain worker picking up an outbox row, an ingestion coroutine still in
    flight -- lands after the next test's setup wipe, referencing a user that
    wipe already removed, and fails on a foreign key. Resetting on teardown
    too removes that window for the common case and keeps each test's debris
    from being visible to the next one at all.

    This fixture used to reset only at setup, on the grounds that ``chat``
    persisted turns with ``asyncio.create_task``. That is no longer true: the
    chat endpoints await their save in the request's ``finally`` block
    precisely so the turn is durable before the response ends
    (``app/api/v1/chat.py``). The justification for the weaker isolation is
    gone with it, so the isolation is restored.

    Resetting at teardown cannot by itself make an in-flight detached write
    deterministic -- nothing can, short of removing the detach. What it does
    guarantee is that a test never *starts* by inheriting another test's rows,
    which is the direction the suite actually depends on.
    """
    _reset_database()
    yield
    _reset_database()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator:
    """Yield an AsyncSession whose changes are always rolled back.

    The session is bound to a connection that holds an outer transaction, and that
    transaction is rolled back on teardown. Binding matters: ``AsyncSession.begin()``
    would create the session's *root* transaction, so a ``commit()`` inside a test would
    commit for real and the rollback on teardown would run against a deactivated
    transaction, breaking the guarantee.
    """
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    from app.database import engine  # noqa: PLC0415

    async with engine.connect() as connection:
        outer_transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            if outer_transaction.is_active:
                await outer_transaction.rollback()


_TEST_USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=False)
def mock_current_user():
    """
    Override the get_current_user dependency so chat/idempotency tests can run
    without hitting the real auth/database stack.

    Returns a dict mimicking Authorization headers (the override itself bypasses
    header validation, but the fixture is available for tests that want to pass
    explicit headers).
    """
    from app.auth import get_current_user  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415

    async def _override_get_current_user():
        return _TEST_USER_ID

    app.dependency_overrides[get_current_user] = _override_get_current_user
    yield {"Authorization": f"Bearer test-token-for-{_TEST_USER_ID}"}
    app.dependency_overrides.pop(get_current_user, None)


class MockEmbeddingResponse:
    def __init__(self, num_inputs):
        self.data = [{"embedding": [0.0] * 1536} for _ in range(num_inputs)]


def mock_litellm_embedding(**kwargs):
    inputs = kwargs.get("input", [])
    num_inputs = len(inputs) if isinstance(inputs, list) else 1
    return MockEmbeddingResponse(num_inputs)


_litellm_patcher = patch("litellm.embedding", side_effect=mock_litellm_embedding)
_litellm_patcher.start()
