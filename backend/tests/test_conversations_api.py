"""
Tests for the conversations API endpoints.

Uses mocked database sessions so tests stay fast and isolated from the
real Postgres instance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.conversations import (
    delete_conversation,
    list_conversations,
    rename_conversation,
)
from app.schemas.models import (
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMetadata,
    ConversationRenameRequest,
)


def _make_conversation(user_id: str, **overrides):
    data = {
        "id": uuid.uuid4(),
        "user_id": uuid.UUID(user_id),
        "session_id": f"session-{uuid.uuid4().hex[:8]}",
        "title": "Test conversation",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "messages": [],
        **overrides,
    }
    return data


def _mock_conversation_row(**overrides):
    row = MagicMock()
    row.id = overrides.get("id", uuid.uuid4())
    row.user_id = overrides.get("user_id", uuid.UUID("00000000-0000-0000-0000-000000000001"))
    row.title = overrides.get("title", "Test conversation")
    row.created_at = overrides.get("created_at", datetime.now(UTC))
    row.updated_at = overrides.get("updated_at", datetime.now(UTC))
    row.messages = overrides.get("messages", [])
    row.session_id = overrides.get("session_id", "session-123")
    return row


@pytest.mark.asyncio
async def test_list_conversations_returns_old_and_new():
    """List endpoint should return all conversations, including older ones."""
    user_id = "00000000-0000-0000-0000-000000000001"
    old_conv = _mock_conversation_row(
        user_id=uuid.UUID(user_id), title="Old conversation", updated_at="2026-01-01T00:00:00+00:00"
    )
    new_conv = _mock_conversation_row(
        user_id=uuid.UUID(user_id), title="New conversation", updated_at="2026-01-02T00:00:00+00:00"
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [new_conv, old_conv]

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert isinstance(response, ConversationListResponse)
    assert len(response.conversations) == 2
    titles = [c.title for c in response.conversations]
    assert "Old conversation" in titles
    assert "New conversation" in titles


@pytest.mark.asyncio
async def test_list_conversations_ordered_by_updated_at():
    """Conversations should be returned with the most recently updated first."""
    user_id = "00000000-0000-0000-0000-000000000001"
    first = _mock_conversation_row(
        user_id=uuid.UUID(user_id), title="First", updated_at="2026-01-01T00:00:00+00:00"
    )
    second = _mock_conversation_row(
        user_id=uuid.UUID(user_id), title="Second", updated_at="2026-01-02T00:00:00+00:00"
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [second, first]

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert len(response.conversations) == 2
    assert response.conversations[0].title == "Second"
    assert response.conversations[1].title == "First"


@pytest.mark.asyncio
async def test_list_conversations_scoped_to_user():
    """List endpoint must only return conversations for the authenticated user."""
    user_id = "00000000-0000-0000-0000-000000000001"

    my_conv = _mock_conversation_row(user_id=uuid.UUID(user_id), title="My conversation")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [my_conv]

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert len(response.conversations) == 1
    assert response.conversations[0].title == "My conversation"

    # Verify the query filtered by the correct user_id.
    called_stmt = mock_session.execute.call_args[0][0]
    assert "user_id" in str(called_stmt)


@pytest.mark.asyncio
async def test_get_conversation_by_id_returns_history():
    """Retrieving a conversation by ID should return its message history."""
    user_id = "00000000-0000-0000-0000-000000000001"
    conv_id = uuid.uuid4()

    conv = _mock_conversation_row(
        id=conv_id, user_id=uuid.UUID(user_id), title="History conversation"
    )
    msg1 = MagicMock()
    msg1.id = uuid.uuid4()
    msg1.role = "user"
    msg1.content = "Hello"
    msg1.timestamp = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    msg1.message_metadata = {}

    msg2 = MagicMock()
    msg2.id = uuid.uuid4()
    msg2.role = "assistant"
    msg2.content = "Hi there"
    msg2.timestamp = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    msg2.message_metadata = {"sources": [], "tool_calls": []}

    mock_conv_result = MagicMock()
    mock_conv_result.scalar_one_or_none.return_value = conv

    mock_msg_result = MagicMock()
    mock_msg_result.scalars.return_value.all.return_value = [msg1, msg2]

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [mock_conv_result, mock_msg_result]

    from app.api.v1.conversations import get_conversation

    response = await get_conversation(
        conversation_id=str(conv_id), current_user_id=user_id, session=mock_session
    )
    assert isinstance(response, ConversationDetailResponse)
    assert response.id == conv_id
    assert response.title == "History conversation"
    assert len(response.messages) == 2
    assert response.messages[0]["role"] == "user"
    assert response.messages[0]["content"] == "Hello"
    assert response.messages[1]["role"] == "assistant"
    assert response.messages[1]["content"] == "Hi there"


@pytest.mark.asyncio
async def test_get_conversation_not_found_returns_404():
    """A valid conversation ID that does not belong to the user should return 404."""
    from fastapi import HTTPException

    user_id = "00000000-0000-0000-0000-000000000001"
    conv_id = uuid.uuid4()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    from app.api.v1.conversations import get_conversation

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation(
            conversation_id=str(conv_id), current_user_id=user_id, session=mock_session
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_sidebar_ids_match_list_response():
    """Conversation IDs returned by list should match those used by the sidebar."""
    user_id = "00000000-0000-0000-0000-000000000001"
    conv_a = _mock_conversation_row(
        user_id=uuid.UUID(user_id), title="Conversation A", session_id="a"
    )
    conv_b = _mock_conversation_row(
        user_id=uuid.UUID(user_id), title="Conversation B", session_id="b"
    )

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [conv_a, conv_b]

    detail_result = MagicMock()
    detail_result.scalar_one_or_none.return_value = conv_a

    msg_result = MagicMock()
    msg_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [list_result, detail_result, msg_result]

    list_response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert len(list_response.conversations) == 2
    returned_ids = [str(c.id) for c in list_response.conversations]
    assert str(conv_a.id) in returned_ids
    assert str(conv_b.id) in returned_ids


@pytest.mark.asyncio
async def test_old_conversations_remain_visible_across_sessions():
    """Conversations created earlier should still appear in later list calls."""
    user_id = "00000000-0000-0000-0000-000000000001"

    old_conv = _mock_conversation_row(
        user_id=uuid.UUID(user_id),
        title="Old conversation",
        updated_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    new_conv = _mock_conversation_row(
        user_id=uuid.UUID(user_id),
        title="New conversation",
        updated_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [new_conv, old_conv]

    mock_session = AsyncMock()
    mock_session.execute.return_value = list_result

    # First session: list conversations
    first_response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert len(first_response.conversations) == 2
    old_ids = [c.id for c in first_response.conversations]
    assert str(old_conv.id) in [str(i) for i in old_ids]

    # Second session: list again with a fresh mock result
    list_result2 = MagicMock()
    list_result2.scalars.return_value.all.return_value = [new_conv, old_conv]
    mock_session2 = AsyncMock()
    mock_session2.execute.return_value = list_result2

    second_response = await list_conversations(
        q=None, current_user_id=user_id, session=mock_session2
    )
    assert len(second_response.conversations) == 2
    second_ids = [c.id for c in second_response.conversations]
    assert str(old_conv.id) in [str(i) for i in second_ids]
    assert str(new_conv.id) in [str(i) for i in second_ids]


@pytest.mark.asyncio
async def test_sidebar_click_id_returns_correct_conversation():
    """IDs shown in the sidebar should resolve to the correct conversation detail."""
    user_id = "00000000-0000-0000-0000-000000000001"
    conv_id = uuid.uuid4()

    conv = _mock_conversation_row(id=conv_id, user_id=uuid.UUID(user_id), title="Sidebar click")

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [conv]

    detail_result = MagicMock()
    detail_result.scalar_one_or_none.return_value = conv

    msg_result = MagicMock()
    msg_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [list_result, detail_result, msg_result]

    # List conversations (sidebar)
    list_response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert len(list_response.conversations) == 1
    sidebar_conv = list_response.conversations[0]
    assert sidebar_conv.title == "Sidebar click"

    # Click the conversation ID from the sidebar
    from app.api.v1.conversations import get_conversation

    detail_response = await get_conversation(
        conversation_id=str(sidebar_conv.id),
        current_user_id=user_id,
        session=mock_session,
    )
    assert detail_response.id == sidebar_conv.id
    assert detail_response.title == "Sidebar click"


@pytest.mark.asyncio
async def test_new_chat_does_not_break_existing_sidebar_conversations():
    """Creating a new chat should not remove or corrupt existing conversations."""
    user_id = "00000000-0000-0000-0000-000000000001"

    existing = _mock_conversation_row(
        user_id=uuid.UUID(user_id),
        title="Existing conversation",
        updated_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    new_chat = _mock_conversation_row(
        user_id=uuid.UUID(user_id),
        title="New chat",
        updated_at=datetime(2026, 1, 3, 0, 0, 0, tzinfo=UTC),
    )

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [new_chat, existing]

    mock_session = AsyncMock()
    mock_session.execute.return_value = list_result

    response = await list_conversations(q=None, current_user_id=user_id, session=mock_session)
    assert len(response.conversations) == 2
    titles = [c.title for c in response.conversations]
    assert "Existing conversation" in titles
    assert "New chat" in titles


@pytest.mark.asyncio
async def test_rename_conversation_stores_trimmed_title_and_returns_metadata():
    """Renaming updates the row and echoes the new conversation metadata back."""
    user_id = "00000000-0000-0000-0000-000000000001"
    conv = _mock_conversation_row(user_id=uuid.UUID(user_id), title="Untitled claim chat")

    owned_result = MagicMock()
    owned_result.scalar_one_or_none.return_value = conv

    mock_session = AsyncMock()
    mock_session.execute.return_value = owned_result

    response = await rename_conversation(
        conversation_id=str(conv.id),
        payload=ConversationRenameRequest(title="  Spring flood claim  "),
        current_user_id=user_id,
        session=mock_session,
    )

    assert isinstance(response, ConversationMetadata)
    assert response.title == "Spring flood claim"
    assert response.id == conv.id
    assert conv.title == "Spring flood claim"
    mock_session.commit.assert_awaited_once()
    # The echoed metadata is the in-memory row state after the commit: the
    # session factory runs with expire_on_commit=False, so the timestamp the
    # column's onupdate assigned is already on the instance.
    assert response.created_at == conv.created_at
    assert response.updated_at == conv.updated_at


@pytest.mark.parametrize("title", ["", "   ", "x" * 256])
def test_rename_conversation_rejects_blank_and_over_length_titles(title):
    """The request model refuses blank and over-long titles before the handler runs.

    FastAPI validates the body against this model, so the ``ValidationError``
    raised here is exactly what a caller receives as a 422.
    """
    with pytest.raises(ValidationError):
        ConversationRenameRequest(title=title)


def test_rename_conversation_accepts_a_title_at_the_column_limit():
    """A title exactly as long as the column allows is accepted and kept as sent."""
    title = "x" * 255
    assert ConversationRenameRequest(title=title).title == title


@pytest.mark.asyncio
async def test_rename_conversation_malformed_id_is_400():
    """A non-UUID conversation id is refused before any query runs."""
    mock_session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await rename_conversation(
            conversation_id="not-a-uuid",
            payload=ConversationRenameRequest(title="Renamed"),
            current_user_id="00000000-0000-0000-0000-000000000001",
            session=mock_session,
        )

    assert exc_info.value.status_code == 400
    mock_session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_conversation_not_owned_is_404():
    """Another user's conversation is refused exactly like one that never existed."""
    user_id = "00000000-0000-0000-0000-000000000002"

    missing_result = MagicMock()
    missing_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute.return_value = missing_result

    with pytest.raises(HTTPException) as exc_info:
        await rename_conversation(
            conversation_id=str(uuid.uuid4()),
            payload=ConversationRenameRequest(title="Renamed"),
            current_user_id=user_id,
            session=mock_session,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Conversation not found"
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_conversation_removes_messages_then_conversation():
    """A delete drops the message rows first, in the same transaction as the row.

    The messages are deleted explicitly and before the conversation itself, so
    the behaviour does not depend on the FK cascade a given migration applied,
    and the conversation's agent session is released once the row is gone.
    """
    user_id = "00000000-0000-0000-0000-000000000001"
    conv = _mock_conversation_row(user_id=uuid.UUID(user_id), title="Doomed conversation")

    owned_result = MagicMock()
    owned_result.scalar_one_or_none.return_value = conv

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [owned_result, MagicMock(), MagicMock()]

    with patch("app.api.v1.conversations.delete_user_session", new=AsyncMock()) as mock_release:
        response = await delete_conversation(
            conversation_id=str(conv.id),
            current_user_id=user_id,
            session=mock_session,
        )

    # The route declares a 204 with no body.
    assert response is None

    statements = [call.args[0] for call in mock_session.execute.call_args_list]
    deletes = [
        str(statement).split(" WHERE ")[0]
        for statement in statements
        if str(statement).startswith("DELETE FROM")
    ]
    assert deletes == ["DELETE FROM conversation_messages", "DELETE FROM conversations"]
    mock_session.commit.assert_awaited_once()

    # The agent session of the deleted conversation is released after the
    # commit, so the next message cannot resume the deleted transcript.
    mock_release.assert_awaited_once_with(user_id, conv.session_id)

    # With the row gone, the detail request for the same id 404s.
    missing_result = MagicMock()
    missing_result.scalar_one_or_none.return_value = None
    after_delete = AsyncMock()
    after_delete.execute.return_value = missing_result

    from app.api.v1.conversations import get_conversation

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation(
            conversation_id=str(conv.id),
            current_user_id=user_id,
            session=after_delete,
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_conversation_not_owned_is_404():
    """A conversation belonging to another user is deleted by no query at all."""
    user_id = "00000000-0000-0000-0000-000000000002"

    missing_result = MagicMock()
    missing_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute.return_value = missing_result

    with patch("app.api.v1.conversations.delete_user_session", new=AsyncMock()) as mock_release:
        with pytest.raises(HTTPException) as exc_info:
            await delete_conversation(
                conversation_id=str(uuid.uuid4()),
                current_user_id=user_id,
                session=mock_session,
            )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Conversation not found"
    mock_session.execute.assert_awaited_once()
    mock_session.commit.assert_not_awaited()
    mock_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_conversation_malformed_id_is_400():
    """A non-UUID conversation id is refused before any query runs."""
    mock_session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await delete_conversation(
            conversation_id="not-a-uuid",
            current_user_id="00000000-0000-0000-0000-000000000001",
            session=mock_session,
        )

    assert exc_info.value.status_code == 400
    mock_session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_conversations_filters_by_title_when_query_given():
    """A query filters the titles case-insensitively and leaves the shape intact."""
    user_id = "00000000-0000-0000-0000-000000000001"
    water = _mock_conversation_row(user_id=uuid.UUID(user_id), title="Water damage claim")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [water]

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    response = await list_conversations(q="water", current_user_id=user_id, session=mock_session)

    assert isinstance(response, ConversationListResponse)
    assert [conversation.title for conversation in response.conversations] == ["Water damage claim"]

    compiled = mock_session.execute.call_args[0][0].compile()
    assert "LIKE" in str(compiled).upper()
    assert "%water%" in compiled.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, ""])
async def test_list_conversations_without_query_returns_everything(query):
    """An omitted or empty query skips the title filter and returns everything."""
    user_id = "00000000-0000-0000-0000-000000000001"
    first = _mock_conversation_row(user_id=uuid.UUID(user_id), title="Water damage claim")
    second = _mock_conversation_row(user_id=uuid.UUID(user_id), title="Policy limits")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [first, second]

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result

    response = await list_conversations(q=query, current_user_id=user_id, session=mock_session)

    assert len(response.conversations) == 2
    compiled = mock_session.execute.call_args[0][0].compile()
    assert "LIKE" not in str(compiled).upper()
