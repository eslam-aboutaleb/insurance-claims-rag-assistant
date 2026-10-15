from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# Importing the models package imports every model module, which is what
# registers all tables on Base.metadata for autogenerate. One import cannot
# drift the way a hand-maintained list of model modules did.
import app.models  # noqa: F401
from alembic import context
from app.core.database import DATABASE_URL
from app.models.base import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target_metadata to Base.metadata so that Alembic can compare the
# database schema against the ORM model definitions during autogenerate.
target_metadata = Base.metadata


def _get_database_url() -> str:
    """Resolve the database URL from the application settings.

    Uses the same Pydantic settings source as the running application so
    that migrations and the app never drift on connection details.
    """
    return DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well.  By skipping the Engine creation we
    don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Any) -> None:
    """Run migrations using a synchronous connection (called via run_sync)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an async engine and delegates migration execution to a sync
    function via connection.run_sync(), which is the SQLAlchemy-recommended
    pattern for running sync-only Alembic operations over an async connection.
    """
    database_url = _get_database_url()
    connectable = create_async_engine(
        database_url,
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
