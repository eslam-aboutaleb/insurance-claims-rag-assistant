import { test, expect } from "@playwright/test";
import {
  APP_URL,
  FIRST_CONVERSATION_MESSAGE,
  SECOND_CONVERSATION_MESSAGE,
  findByTitle,
  seedUserWithTwoConversations,
  signIn,
} from "./helpers";

// The transcript and the sidebar row render the same title, so the assertions
// that are about what the chat window showed are scoped to it rather than to the
// whole page. The shared helper seeds deterministic database fixtures and
// addresses conversations by title, the same way the other specs do.
test.describe.configure({ timeout: 90_000 });

test("old conversations stay listed in the sidebar and open at /chat/:id", async ({
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
  await expect(history).not.toContainText("No conversations yet");
  await expect(history).toContainText(first.title);
  await expect(history).toContainText(second.title);

  await history.getByText(first.title).click();

  await expect(page).toHaveURL(`${APP_URL}/chat/${first.id}`);
  await expect(
    page.getByText("This is an archived chat", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByTestId("message-list").getByText(first.title),
  ).toBeVisible();

  // A full page load on the deep link must still resolve the conversation.
  await page.reload();
  await expect(page).toHaveURL(`${APP_URL}/chat/${first.id}`);
  await expect(
    page.getByTestId("message-list").getByText(first.title),
  ).toBeVisible();

  // Both conversations remain reachable, and the other one loads its own history.
  await page.getByTestId("chat-history").getByText(second.title).click();
  await expect(page).toHaveURL(`${APP_URL}/chat/${second.id}`);
  await expect(
    page.getByTestId("message-list").getByText(second.title),
  ).toBeVisible();
  await expect(
    page.getByTestId("message-list").getByText(first.title),
  ).toHaveCount(0);
});

test("New Chat clears the view but keeps old conversations in the sidebar", async ({
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

  await history.getByText(first.title).click();
  await expect(page).toHaveURL(`${APP_URL}/chat/${first.id}`);

  await page.getByTestId("new-chat-button").click();

  await expect(page).toHaveURL(`${APP_URL}/chat`);
  await expect(history.getByRole("button")).toHaveCount(2);
  await expect(history).toContainText(first.title);
  await expect(history).toContainText(second.title);
});
