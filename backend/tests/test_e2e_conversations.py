"""
E2E test for conversation sidebar functionality.

Verifies:
1. Conversations are created when sending a chat message
2. Old conversations are retrieved from the API
3. Conversation IDs from the list match what the sidebar uses
4. The conversation detail endpoint returns the correct data for a given ID
5. Multiple messages in the same session belong to the same conversation
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import aiohttp
import pytest


def _server_available(api_url: str) -> bool:
    """Return True when a backend is listening on ``api_url``.

    This test drives a deployed stack over HTTP rather than the in-process app, so it
    must skip instead of fail when nothing is running. Set OMNICARE_E2E_API_URL to point
    it at a non-default host.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(api_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


async def _signup(session: aiohttp.ClientSession, api_url: str) -> str:
    """Sign up a fresh user and return their access token."""
    username = f"e2e_{uuid.uuid4().hex[:8]}"
    password = "TestPass123!"
    signup_resp = await session.post(
        f"{api_url}/api/v1/auth/signup",
        json={"username": username, "password": password},
    )
    assert signup_resp.status == 201, f"Signup failed: {signup_resp.status}"
    signup_data = await signup_resp.json()
    return signup_data["access_token"]


async def _list_conversations(
    session: aiohttp.ClientSession, api_url: str, token: str
) -> list[dict[str, Any]]:
    """Return the caller's conversation list, newest first."""
    list_resp = await session.get(
        f"{api_url}/api/v1/chat/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_resp.status == 200
    list_data = await list_resp.json()
    return list_data.get("conversations", [])


async def _send_message(
    session: aiohttp.ClientSession, api_url: str, token: str, message: str
) -> None:
    """Send one chat message and wait for its background persistence."""
    chat_resp = await session.post(
        f"{api_url}/api/v1/chat/stream",
        json={"message": message},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert chat_resp.status == 200, f"Chat failed: {chat_resp.status}"
    await asyncio.sleep(3)


async def _get_conversation(
    session: aiohttp.ClientSession, api_url: str, token: str, conversation_id: str
) -> dict[str, Any]:
    """Return the conversation detail payload for ``conversation_id``."""
    detail_resp = await session.get(
        f"{api_url}/api/v1/chat/conversations/{conversation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail_resp.status == 200, f"Get conversation failed: {detail_resp.status}"
    return await detail_resp.json()


@pytest.mark.asyncio
async def test_e2e_conversations_sidebar_and_url():
    """E2E test: old conversations are retrieved and URL updates with conversation ID."""
    import os

    API_URL = os.environ.get("OMNICARE_E2E_API_URL", "http://localhost:8000")

    if not _server_available(API_URL):
        pytest.skip(
            f"No backend reachable at {API_URL}. "
            "Start the stack (docker compose up -d) to run this test."
        )

    async with aiohttp.ClientSession() as session:
        token = await _signup(session, API_URL)

        # Step 2: List conversations (should be empty initially)
        conversations_before = await _list_conversations(session, API_URL, token)
        assert len(conversations_before) == 0, "Expected no conversations before chat"

        # Step 3: Send a chat message (creates a conversation)
        await _send_message(session, API_URL, token, "E2E test conversation")

        # Step 4: List conversations (should have 1)
        conversations = await _list_conversations(session, API_URL, token)
        assert len(conversations) == 1, f"Expected 1 conversation, got {len(conversations)}"

        conversation_id = conversations[0]["id"]
        conversation_title = conversations[0]["title"]
        assert conversation_title == "E2E test conversation", "Expected correct conversation title"

        # Step 5: Get conversation by ID (simulates clicking in sidebar / URL update)
        detail_data = await _get_conversation(session, API_URL, token, conversation_id)
        assert detail_data["id"] == conversation_id, "ID mismatch in detail response"
        assert detail_data["title"] == conversation_title, "Title mismatch in detail response"
        # Each chat turn creates both a user message and an assistant response
        assert (
            len(detail_data["messages"]) == 2
        ), f"Expected 2 messages in conversation, got {len(detail_data['messages'])}"

        # Step 6: Send another message in the same session
        await _send_message(session, API_URL, token, "Second message in same conversation")

        # Step 7: Verify the conversation list still shows 1 conversation
        # (same session = same conversation)
        conversations2 = await _list_conversations(session, API_URL, token)
        assert len(conversations2) == 1, "Expected still 1 conversation (same session)"

        detail_data2 = await _get_conversation(session, API_URL, token, conversation_id)
        assert detail_data2["id"] == conversation_id, "Conversation not accessible by ID"
        # After second message: 2 user + 2 assistant = 4 messages total
        assert (
            len(detail_data2["messages"]) == 4
        ), f"Expected 4 messages in conversation, got {len(detail_data2['messages'])}"

        # Step 8: Reset session and create a new conversation
        reset_resp = await session.post(
            f"{API_URL}/api/v1/chat/reset",
            headers={"Authorization": f"Bearer {token}"},
        )
        # Reset might fail if not implemented, that's ok
        if reset_resp.status != 200:
            pass

        await _send_message(session, API_URL, token, "New conversation after reset")

        # Step 9: Verify we now have 2 conversations
        conversations3 = await _list_conversations(session, API_URL, token)
        assert (
            len(conversations3) == 2
        ), f"Expected 2 conversations after reset, got {len(conversations3)}"

        # Verify both conversations are accessible by their IDs
        conv_ids = [c["id"] for c in conversations3]
        assert conversation_id in conv_ids, "First conversation missing after reset"
        new_conv_id = [c["id"] for c in conversations3 if c["id"] != conversation_id][0]

        # Verify the first conversation still has its messages
        detail_data3 = await _get_conversation(session, API_URL, token, conversation_id)
        assert len(detail_data3["messages"]) == 4, "First conversation should still have 4 messages"

        print("✅ All E2E tests passed!")
        print(f"   First conversation ID: {conversation_id}")
        print(f"   First conversation URL: /chat/{conversation_id}")
        print(f"   Second conversation ID: {new_conv_id}")
        print(f"   Second conversation URL: /chat/{new_conv_id}")
        print("   Old conversations remain visible after reset: True")
