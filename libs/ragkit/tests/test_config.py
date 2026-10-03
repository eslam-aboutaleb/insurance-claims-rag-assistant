"""Structural check that the host application's settings satisfy RagSettings."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ragkit.config import RagSettings

BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"


def test_backend_settings_satisfy_rag_settings_protocol() -> None:
    """The backend's Settings must structurally satisfy the RagSettings protocol."""
    if not (BACKEND_DIR / "app" / "config.py").is_file():
        pytest.skip("backend app config is not available in this environment")
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    try:
        from app.config import get_settings
    except Exception as exc:
        pytest.skip(f"backend app config could not be imported: {exc}")

    settings = get_settings()
    assert isinstance(settings, RagSettings)
