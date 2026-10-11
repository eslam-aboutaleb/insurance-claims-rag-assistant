import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.conversation_store import SavedMessageIds


@pytest.mark.asyncio
async def test_chat_stream_empty_message(test_client, mock_current_user):
    response = test_client.post(
        "/api/v1/chat/stream", json={"message": ""}, headers=mock_current_user
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_chat_stream_invalid_json(test_client, mock_current_user):
    response = test_client.post("/api/v1/chat/stream", data="invalid", headers=mock_current_user)
    assert response.status_code == 422


@pytest.mark.asyncio
@patch("app.api.v1.chat_stream.run_agent_stream")
async def test_chat_stream_valid(mock_run_agent_stream, test_client, mock_current_user):
    async def mock_run(*args, **kwargs):
        yield 'data: {"type": "content", "content": "Hello"}\n\n'

    mock_run_agent_stream.return_value = mock_run()

    response = test_client.post(
        "/api/v1/auth/signup",
        json={"username": f"stream_user_{uuid.uuid4().hex[:8]}", "password": "TestPass123!"},
    )
    token = response.json()["access_token"]

    response2 = test_client.post(
        "/api/v1/chat/stream", json={"message": "Hi"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response2.status_code == 200


@pytest.mark.asyncio
@patch("app.api.v1.chat_stream.save_conversation_turn", new_callable=AsyncMock)
async def test_chat_stream_persists_conversation_on_llm_failure(
    mock_save_conversation_turn,
    test_client,
    mock_current_user,
):
    """Conversation turn must be persisted even when the LLM stream raises."""

    async def failing_run(user_id, message, result):
        result.session_id = "session-123"
        yield 'data: {"type": "content", "content": "Hello"}\n\n'
        raise Exception("LLM failure")

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.side_effect = failing_run

        response = test_client.post(
            "/api/v1/auth/signup",
            json={
                "username": f"stream_fail_user_{uuid.uuid4().hex[:8]}",
                "password": "TestPass123!",
            },
        )
        token = response.json()["access_token"]

        response2 = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "Hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response2.status_code == 200
        assert "Streaming failed" in response2.text
        mock_save_conversation_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_stream_emits_response_complete_on_error(test_client, mock_current_user):
    """Frontend must receive response_complete even when the LLM stream fails."""

    async def failing_run(user_id, message, result):
        result.session_id = "session-456"
        yield 'data: {"type": "content", "content": "Hello"}\n\n'
        raise Exception("LLM failure")

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.side_effect = failing_run

        response = test_client.post(
            "/api/v1/auth/signup",
            json={
                "username": f"stream_complete_user_{uuid.uuid4().hex[:8]}",
                "password": "TestPass123!",
            },
        )
        token = response.json()["access_token"]

        response2 = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "Hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response2.status_code == 200
        body = response2.text
        assert "response_complete" in body
        assert "Streaming failed" in body


@pytest.mark.asyncio
async def test_chat_stream_creates_conversation_visible_in_sidebar(
    test_client,
):
    """A chat turn should create a conversation that appears in the sidebar list."""

    async def mock_run(user_id, message, result):
        result.session_id = "session-sidebar"
        yield 'data: {"type": "text_delta", "content": {"parts": [{"text": "Hello"}]}}\n\n'

    with (
        patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream,
        patch(
            "app.api.v1.chat_stream._persist_streamed_turn", new_callable=AsyncMock
        ) as mock_persist,
    ):
        mock_run_agent_stream.side_effect = mock_run
        # The terminal frame reads the ids off the return value, so the mock
        # must hand back the same shape the real helper returns.
        mock_persist.return_value = SavedMessageIds(
            user_message_id="11111111-1111-1111-1111-111111111111",
            assistant_message_id="22222222-2222-2222-2222-222222222222",
        )

        response = test_client.post(
            "/api/v1/auth/signup",
            json={"username": f"sidebar_user_{uuid.uuid4().hex[:8]}", "password": "TestPass123!"},
        )
        token = response.json()["access_token"]

        response2 = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "Hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response2.status_code == 200
        mock_persist.assert_awaited_once()
        # The ids the persistence reported ride on the terminal frame.
        assert "22222222-2222-2222-2222-222222222222" in response2.text
        assert "11111111-1111-1111-1111-111111111111" in response2.text
