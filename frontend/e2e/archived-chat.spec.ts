/**
 * Archived-conversation behaviour of the chat window (T15 integrity work, T16
 * read-only guard).
 *
 * Stack requirement: the full stack must be up -- frontend on
 * http://localhost:3000, backend on http://localhost:8000. The conversations
 * are seeded by driving the real chat API, so the specs exercise genuine
 * persisted history rather than mocked message lists. The "no messages yet"
 * case additionally needs the docker CLI, because the chat API cannot create a
 * conversation without a turn; that fixture is inserted through the backend
 * container and the test is skipped rather than failed when docker is absent.
 */

import { test, expect } from "@playwright/test";
import {
  APP_URL,
  SECOND_CONVERSATION_MESSAGE,
  WELCOME_CHIP_LABELS,
  findByTitle,
  gotoWithSession,
  seedEmptyConversation,
  seedUserWithTwoConversations,
  signIn,
} from "./helpers";

test.describe("archived conversation", () => {
  test("a deep link is read-only and offers no suggestion chips", async ({
    page,
    request,
  }) => {
    const { username, token, conversations } =
      await seedUserWithTwoConversations(request);
    const second = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);

    await signIn(page, username);
    await gotoWithSession(page, `${APP_URL}/chat/${second.id}`, token);

    // The history itself still loads and reads. Scoped to the transcript: the
    // sidebar row carries the same string as its title.
    await expect(
      page.getByTestId("message-list").getByText(SECOND_CONVERSATION_MESSAGE),
    ).toBeVisible();

    // Read-only is a mode, not a hint: the notice replaces the composer.
    await expect(
      page.getByText("This is an archived chat", { exact: false }),
    ).toBeVisible();
    await expect(page.getByTestId("chat-textarea")).toHaveCount(0);
    await expect(page.getByTestId("send-button")).toHaveCount(0);

    // The new chat's welcome section -- and every chip in it -- belongs to a
    // conversation that can actually accept a message.
    await expect(page.getByTestId("empty-state")).toHaveCount(0);
    for (const chip of WELCOME_CHIP_LABELS) {
      await expect(page.getByRole("button", { name: chip })).toHaveCount(0);
    }
  });

  test("the loading state holds while history is in flight and chips never appear", async ({
    page,
    request,
  }) => {
    const { username, token, conversations } =
      await seedUserWithTwoConversations(request);
    const second = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);

    await signIn(page, username);
    // Land on the new chat first so the sidebar list is already loaded and the
    // only in-flight request is the one being held open.
    await expect(page.getByTestId("chat-history")).toBeVisible();

    let releaseHistoryRequest: () => void = () => {};
    const historyGate = new Promise<void>((resolve) => {
      releaseHistoryRequest = resolve;
    });
    const gateGuard = setTimeout(releaseHistoryRequest, 20_000);

    // Only the history read for this one conversation is held; the PATCH and
    // DELETE verbs share the path, so they are answered normally.
    await page.route(
      `**/api/v1/chat/conversations/${second.id}`,
      async (route) => {
        if (route.request().method() !== "GET") {
          await route.continue();
          return;
        }
        await historyGate;
        await route.continue();
      },
    );

    try {
      await gotoWithSession(page, `${APP_URL}/chat/${second.id}`, token);

      const loading = page.getByTestId("history-loading");
      await expect(loading).toBeVisible();
      await expect(loading).toContainText("Loading conversation");

      // A conversation that cannot accept a message is never offered chips for
      // one, not even for the moment its history takes to arrive.
      await expect(page.getByTestId("empty-state")).toHaveCount(0);
      for (const chip of WELCOME_CHIP_LABELS) {
        await expect(page.getByRole("button", { name: chip })).toHaveCount(0);
      }
      await expect(page.getByTestId("chat-textarea")).toHaveCount(0);
      await expect(page.getByTestId("message-list")).toHaveCount(0);
    } finally {
      clearTimeout(gateGuard);
      releaseHistoryRequest();
    }

    // Releasing the request resolves into the real history.
    await expect(page.getByTestId("history-loading")).toHaveCount(0);
    await expect(
      page.getByTestId("message-list").getByText(SECOND_CONVERSATION_MESSAGE),
    ).toBeVisible();
  });

  test("an archived conversation with no messages says so", async ({
    page,
    request,
  }) => {
    const seed = await seedEmptyConversation(request);
    if (!seed) {
      test.skip();
    }
    const { username, token, conversationId } = seed!;

    await signIn(page, username);
    await gotoWithSession(page, `${APP_URL}/chat/${conversationId}`, token);

    // Neither the welcome section nor its chips: the conversation is simply
    // empty, and saying so is the honest state for it.
    const empty = page.getByTestId("empty-archived-state");
    await expect(empty).toBeVisible();
    await expect(empty).toContainText("This conversation has no messages yet.");
    await expect(page.getByTestId("message-list")).toHaveCount(0);
    await expect(page.getByTestId("empty-state")).toHaveCount(0);
    for (const chip of WELCOME_CHIP_LABELS) {
      await expect(page.getByRole("button", { name: chip })).toHaveCount(0);
    }

    // Still read-only, and still listed, so the empty conversation is reachable
    // rather than a dead link.
    await expect(
      page.getByText("This is an archived chat", { exact: false }),
    ).toBeVisible();
    await expect(page.getByTestId("chat-history")).toContainText(
      "Empty archived conversation",
    );
  });
});
