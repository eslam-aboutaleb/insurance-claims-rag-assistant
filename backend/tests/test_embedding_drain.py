"""
Tests for the OmniCare embedding drainer entry point.

The drain loop itself moved to ``ragit.jobs.drainer``
(ragit plan 05) and is tested in
``libs/ragit/tests/test_embedding_drain.py``. These
tests pin the app-side wiring: the thin ``main()``
constructs the OmniCare store and claim processor and
hands them, with the validated settings, to ragit's
drainer.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.workers import embedding_drain


def test_main_wires_app_store_and_processor_into_ragit_drainer() -> None:
    """The entry point builds the OmniCare store and processor and
    passes the validated drain settings to ragit's drainer."""
    from app.config import settings
    from app.domain.claims.outbox import ClaimJobProcessor, SqlAlchemyJobStore

    with patch.object(embedding_drain, "_ragit_main", return_value=0) as mock_main:
        assert embedding_drain.main() == 0

    args, kwargs = mock_main.call_args
    store, processor = args
    assert isinstance(store, SqlAlchemyJobStore)
    assert isinstance(processor, ClaimJobProcessor)
    assert kwargs["interval"] == settings.embedding_drain_interval_seconds
    assert kwargs["batch_size"] == settings.embedding_drain_batch_size
    assert kwargs["worker_id"] == settings.worker_id


def test_drain_config_comes_from_validated_settings() -> None:
    """The drainer reads its configuration from Settings, not from raw os.environ."""
    from app.config import settings

    assert settings.embedding_drain_interval_seconds > 0.1
    assert settings.embedding_drain_batch_size > 0


def test_drain_settings_reject_out_of_range_values() -> None:
    """Settings enforce the documented bounds rather than clamping at runtime."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(embedding_drain_interval_seconds=0)

    with pytest.raises(ValidationError):
        Settings(embedding_drain_batch_size=0)
