"""Coverage tests for app.main uncovered paths."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMain:
    def test_run_alembic_migrations_failure_raises(self):
        from app.main import _run_alembic_migrations

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "migration failed"
            with pytest.raises(RuntimeError, match="Database migration failed"):
                asyncio.run(_run_alembic_migrations())

    def test_run_alembic_migrations_exception_raises(self):
        from app.main import _run_alembic_migrations

        with patch("subprocess.run", side_effect=Exception("subprocess error")):
            with pytest.raises(RuntimeError, match="Failed to run database migrations"):
                asyncio.run(_run_alembic_migrations())

    def test_run_alembic_migrations_success_with_stdout(self):
        from app.main import _run_alembic_migrations

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "migration applied"
            asyncio.run(_run_alembic_migrations())

    def test_lifespan_ingest_policy_failure_continues(self):
        from app.main import lifespan

        app = MagicMock()
        with patch("app.main.ingest_policy", new_callable=AsyncMock) as mock_ingest:
            mock_ingest.side_effect = Exception("ingest failed")
            with patch("app.main._run_alembic_migrations"):
                gen = lifespan(app)
                asyncio.run(gen.__aenter__())
                asyncio.run(gen.__aexit__(None, None, None))
