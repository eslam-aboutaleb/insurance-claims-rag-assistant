"""
Tests for app.agent.agent module.

Covers session eviction and run_agent/run_agent_stream
source/citation extraction paths.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.agent import reset_user_session, run_agent, run_agent_stream


@pytest.mark.asyncio
async def test_reset_user_session():
    from app.agent.agent import _user_sessions

    _user_sessions["test-user"] = "session-123"
    await reset_user_session("test-user")
    assert "test-user" not in _user_sessions


@pytest.mark.asyncio
async def test_run_agent_extracts_sources_and_citations():
    user_id = str(uuid.uuid4())
    mock_event = MagicMock()
    mock_event.get_function_calls.return_value = None
    mock_event.get_function_responses.return_value = None
    mock_event.is_final_response.return_value = True
    mock_event.content.parts = [MagicMock(text="Final response text")]

    async def mock_run_async(**kwargs):
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = mock_run_async

    with patch("app.agent.agent.runner", mock_runner):
        with patch("app.agent.agent.session_service") as mock_session_service:
            mock_session_service.create_session = AsyncMock()
            mock_session_service.delete_session = AsyncMock()

            result = await run_agent(user_id=user_id, message="test")

    assert result["response"] == "Final response text"
    assert result["sources"] == []
    assert result["tool_calls"] == []
    assert result["session_id"] is not None


@pytest.mark.asyncio
async def test_run_agent_collects_tool_calls_and_sources():
    user_id = str(uuid.uuid4())
    mock_fc = MagicMock()
    mock_fc.name = "query_policy"
    mock_fc.args = {"query": "test"}

    mock_fr = MagicMock()
    mock_fr.name = "query_policy"
    mock_fr.response = {
        "sources": [{"section": "Section 1", "source": "policy.md"}],
        "citation": "Claim CLM-123 in system",
    }

    mock_event_fc = MagicMock()
    mock_event_fc.get_function_calls.return_value = [mock_fc]
    mock_event_fc.get_function_responses.return_value = None
    mock_event_fc.is_final_response.return_value = False
    mock_event_fc.content = None

    mock_event_fr = MagicMock()
    mock_event_fr.get_function_calls.return_value = None
    mock_event_fr.get_function_responses.return_value = [mock_fr]
    mock_event_fr.is_final_response.return_value = True
    mock_event_fr.content.parts = [MagicMock(text="done")]

    async def mock_run_async(**kwargs):
        yield mock_event_fc
        yield mock_event_fr

    mock_runner = MagicMock()
    mock_runner.run_async = mock_run_async

    with patch("app.agent.agent.runner", mock_runner):
        with patch("app.agent.agent.session_service") as mock_session_service:
            mock_session_service.create_session = AsyncMock()
            mock_session_service.delete_session = AsyncMock()

            result = await run_agent(user_id=user_id, message="test")

    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "query_policy"
    assert len(result["sources"]) == 2
    assert "Section 1 (policy.md)" in result["sources"]
    assert "Claim CLM-123 in system" in result["sources"]


@pytest.mark.asyncio
async def test_run_agent_collects_string_sources():
    user_id = str(uuid.uuid4())
    mock_fr = MagicMock()
    mock_fr.name = "query_policy"
    mock_fr.response = {
        "sources": ["string source 1", "string source 2"],
    }

    mock_event_fr = MagicMock()
    mock_event_fr.get_function_calls.return_value = None
    mock_event_fr.get_function_responses.return_value = [mock_fr]
    mock_event_fr.is_final_response.return_value = True
    mock_event_fr.content.parts = [MagicMock(text="done")]

    async def mock_run_async(**kwargs):
        yield mock_event_fr

    mock_runner = MagicMock()
    mock_runner.run_async = mock_run_async

    with patch("app.agent.agent.runner", mock_runner):
        with patch("app.agent.agent.session_service") as mock_session_service:
            mock_session_service.create_session = AsyncMock()
            mock_session_service.delete_session = AsyncMock()

            result = await run_agent(user_id=user_id, message="test")

    assert len(result["sources"]) == 2
    assert "string source 1" in result["sources"]
    assert "string source 2" in result["sources"]


@pytest.mark.asyncio
async def test_run_agent_stream_yields_events():
    user_id = str(uuid.uuid4())
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content.parts = [MagicMock(text="streamed response")]
    mock_event.model_dump_json.return_value = '{"text": "streamed response"}'

    async def mock_run_async(**kwargs):
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = mock_run_async

    with patch("app.agent.agent.runner", mock_runner):
        with patch("app.agent.agent.session_service") as mock_session_service:
            mock_session_service.create_session = AsyncMock()
            mock_session_service.delete_session = AsyncMock()

            chunks = []
            async for chunk in run_agent_stream(user_id=user_id, message="test"):
                chunks.append(chunk)

    assert len(chunks) == 1
    assert "streamed response" in chunks[0]
