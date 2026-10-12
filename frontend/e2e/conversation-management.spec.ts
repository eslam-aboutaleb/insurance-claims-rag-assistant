/**
 * Conversation management through the sidebar (T18).
 *
 * Stack requirement: the full stack must be up -- frontend on
 * http://localhost:3000, backend on http://localhost:8000. Both conversations
 * are seeded by driving the real chat API, so the rename, delete and search
 * flows run against genuine persisted rows and the polling assertions read the
 * server's own view of them.
 *
 * Row actions are mounted only while their row is engaged, so each test engages
 * the row it intends to act on (a focused row keeps its controls, which is what
 * a keyboard user needs) and never asserts a row's button count afterwards.
 */

import { test, expect, type APIRequestContext } from "@playwright/test";
import {
  API_URL,
  APP_URL,
  FIRST_CONVERSATION_MESSAGE,
  SECOND_CONVERSATION_MESSAGE,
  findByTitle,
  seedUserWithTwoConversations,
  signIn,
  type Conversation,
} from "./helpers";

/** Reads one conversation's stored title straight from the API. */
async function storedTitle(
  request: APIRequestContext,
  token: string,
  id: string,
): Promise<string | null> {
  const response = await request.get(`${API_URL}/api/v1/chat/conversations`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.status()).toBe(200);
  const body = (await response.json()).conversations as Conversation[];
  return body.find((conversation) => conversation.id === id)?.title ?? null;
}

test.describe("conversation management", () => {
  test("a conversation can be renamed from the sidebar", async ({
    page,
    request,
  }) => {
    const { username, token, conversations } =
      await seedUserWithTwoConversations(request);
    const target = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);
    const untouched = findByTitle(conversations, FIRST_CONVERSATION_MESSAGE);

    await signIn(page, username);
    await expect(
      page.getByTestId(`conversation-button-${target.id}`),
    ).toBeVisible();

    // Engaging the row is what mounts its actions.
    await page.getByTestId(`conversation-button-${target.id}`).focus();
    const rename = page.getByTestId(`rename-button-${target.id}`);
    await expect(rename).toBeVisible();

    await rename.click();
    const titleField = page.getByTestId(`rename-input-${target.id}`);
    await expect(titleField).toBeVisible();
    await expect(titleField).toHaveValue(target.title);

    await titleField.fill("Renamed water damage chat");
    await titleField.press("Enter");

    await expect(page.getByText("Conversation renamed")).toBeVisible();

    const history = page.getByTestId("chat-history");
    await expect(history).toContainText("Renamed water damage chat");
    await expect(history).not.toContainText(target.title);

    // The list is not just optimistically rewritten: the server stored it.
    await expect
      .poll(() => storedTitle(request, token, target.id), { timeout: 15_000 })
      .toBe("Renamed water damage chat");

    // The other conversation is untouched.
    await expect(history).toContainText(untouched.title);
  });

  test("a non-active conversation can be deleted inline, leaving the open one alone", async ({
    page,
    request,
  }) => {
    const { username, conversations } =
      await seedUserWithTwoConversations(request);
    const open = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);
    const doomed = findByTitle(conversations, FIRST_CONVERSATION_MESSAGE);

    await signIn(page, username);
    await page.getByTestId(`conversation-button-${open.id}`).click();
    await expect(page).toHaveURL(`${APP_URL}/chat/${open.id}`);

    // A delete confirmation is part of the page, not a browser dialog.
    const dialogs: string[] = [];
    page.on("dialog", (dialog) => {
      dialogs.push(dialog.message());
      void dialog.dismiss();
    });

    await page.getByTestId(`conversation-button-${doomed.id}`).focus();
    await page.getByTestId(`delete-button-${doomed.id}`).click();

    const confirmation = page.getByTestId(`delete-confirmation-${doomed.id}`);
    await expect(confirmation).toBeVisible();
    await expect(confirmation).toContainText("Delete this conversation?");
    await expect(page.getByTestId("chat-history")).toContainText(doomed.title);

    await page.getByTestId(`delete-confirm-${doomed.id}`).click();

    await expect(page.getByTestId("chat-history")).not.toContainText(doomed.title);
    await expect(page.getByTestId(`conversation-row-${doomed.id}`)).toHaveCount(0);
    expect(dialogs).toEqual([]);

    // The transcript being read is the one that was open, and it is still open.
    await expect(page).toHaveURL(`${APP_URL}/chat/${open.id}`);
    await expect(
      page.getByTestId("message-list").getByText(SECOND_CONVERSATION_MESSAGE),
    ).toBeVisible();
  });

  test("deleting the active conversation leaves the reader on a fresh chat", async ({
    page,
    request,
  }) => {
    const { username, conversations } =
      await seedUserWithTwoConversations(request);
    const active = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);
    const survivor = findByTitle(conversations, FIRST_CONVERSATION_MESSAGE);

    await signIn(page, username);
    await page.getByTestId(`conversation-button-${active.id}`).click();
    await expect(page).toHaveURL(`${APP_URL}/chat/${active.id}`);
    await expect(
      page.getByText("This is an archived chat", { exact: false }),
    ).toBeVisible();

    await page.getByTestId(`conversation-button-${active.id}`).focus();
    await page.getByTestId(`delete-button-${active.id}`).click();
    await page.getByTestId(`delete-confirm-${active.id}`).click();

    // A deleted transcript is not held on screen: the reader is left on a new
    // chat rather than on a view whose history would 404 on the next load.
    await expect(page).toHaveURL(`${APP_URL}/chat`);
    await expect(page.getByTestId("empty-state")).toBeVisible();
    await expect(page.getByTestId("chat-textarea")).toBeVisible();
    await expect(page.getByTestId(`conversation-row-${active.id}`)).toHaveCount(0);
    await expect(page.getByTestId("chat-history")).toContainText(survivor.title);
  });

  test("the search field filters the list by title substring", async ({
    page,
    request,
  }) => {
    const { username, conversations } =
      await seedUserWithTwoConversations(request);
    const first = findByTitle(conversations, FIRST_CONVERSATION_MESSAGE);
    const second = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);

    await signIn(page, username);
    const history = page.getByTestId("chat-history");
    await expect(history.getByRole("button")).toHaveCount(2);

    const searchQueries: string[] = [];
    await page.route("**/api/v1/chat/conversations**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/v1/chat/conversations") {
        searchQueries.push(url.searchParams.get("q") ?? "");
      }
      await route.continue();
    });

    // The filter is applied server-side, so the request itself has to carry it.
    // "First" is a substring of the first conversation's title only.
    await page.getByTestId("conversation-search").fill("First");
    await expect.poll(() => searchQueries.at(-1)).toBe("First");
    await expect(history.getByRole("button")).toHaveCount(1);
    await expect(history).toContainText(first.title);
    await expect(history).not.toContainText(second.title);

    // A term that matches nothing says so instead of showing an empty list.
    await page.getByTestId("conversation-search").fill("no such conversation");
    await expect(page.getByTestId("no-conversations-match")).toBeVisible();

    // Clearing the filter restores the unfiltered request.
    await page.getByTestId("conversation-search").fill("");
    await expect.poll(() => searchQueries.at(-1)).toBe("");
    await expect(history.getByRole("button")).toHaveCount(2);
    await expect(history).toContainText(first.title);
    await expect(history).toContainText(second.title);
  });
});
