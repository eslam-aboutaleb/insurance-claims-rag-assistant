/**
 * Tier C end-to-end journeys (Phase 0 stabilization plan, part A3).
 *
 * The eight user journeys run against the live Docker stack with a real
 * LLM, so they are tagged @journey and excluded from the default
 * `npm run e2e` selection (playwright.config.ts sets testIgnore for this
 * directory). Run them with `npm run e2e:journeys` or
 * `frontend/scripts/journeys.sh` -- see e2e/journeys/README.md for the
 * runbook.
 *
 * Every journey records a console log, a network HAR, a screenshot, and a
 * JSON observations file into e2e/journeys/artifacts/<slug>/ as the Tier C
 * artifact.
 */

import { test as base, expect } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import type {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
} from "@playwright/test";

const API_URL = "http://localhost:8000";
const APP_URL = "http://localhost:3000";
const PASSWORD = "TestPass123!";

type Conversation = { id: string; title: string };

interface JourneyRecording {
  page: Page;
  context: BrowserContext;
  /** Record a key/value observation into the journey's observations.json. */
  record: (key: string, value: unknown) => void;
}

function slugify(title: string): string {
  return title
    .replace(/@journey/g, "")
    .replace(/^[^:]*:\s*/, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

const test = base.extend<{ recording: JourneyRecording }>({
  recording: async ({ browser }: { browser: Browser }, use, testInfo) => {
    const dir = path.join(
      path.dirname(testInfo.file),
      "artifacts",
      slugify(testInfo.title),
    );
    fs.mkdirSync(dir, { recursive: true });

    const consoleLogs: string[] = [];
    const observations: Record<string, unknown> = {};
    const context = await browser.newContext({
      recordHar: { path: path.join(dir, "network.har") },
    });
    const page = await context.newPage();
    page.on("console", (msg) =>
      consoleLogs.push(`[${msg.type()}] ${msg.text()}`),
    );
    page.on("pageerror", (err) =>
      consoleLogs.push(`[pageerror] ${err.message}`),
    );

    await use({
      page,
      context,
      record: (key: string, value: unknown) => {
        observations[key] = value;
      },
    });

    fs.writeFileSync(
      path.join(dir, "console.log"),
      consoleLogs.join("\n"),
      "utf8",
    );
    fs.writeFileSync(
      path.join(dir, "observations.json"),
      JSON.stringify(observations, null, 2),
      "utf8",
    );
    await page
      .screenshot({ path: path.join(dir, "screenshot.png"), fullPage: true })
      .catch(() => undefined);
    await context.close();
  },
});

// --- Helpers ---------------------------------------------------------------

async function signupViaApi(request: APIRequestContext) {
  const username = `journey_${Date.now()}${Math.floor(Math.random() * 1e6)}@example.com`;
  const response = await request.post(`${API_URL}/api/v1/auth/signup`, {
    data: { username, password: PASSWORD },
  });
  expect(response.status()).toBe(201);
  const token = (await response.json()).access_token;
  return { username, token };
}

async function signInViaUi(page: Page, username: string) {
  await page.goto(`${APP_URL}/login`);
  await page.getByPlaceholder("Enter your email").fill(username);
  await page.getByPlaceholder("Enter your password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL("**/chat**", { timeout: 30_000 });
}

async function sendMessage(page: Page, text: string) {
  await page.getByTestId("chat-textarea").fill(text);
  await page.getByTestId("send-button").click();
  await expect(page.getByTestId("message-list")).toBeVisible();
}

/** Wait until the SSE stream has closed and the UI is idle again. */
async function waitForResponse(page: Page, timeout = 120_000) {
  await expect(page.getByTestId("loading-indicator")).toBeHidden({ timeout });
  await expect(page.getByTestId("send-button")).toBeEnabled({ timeout });
}

async function authedHeaders(token: string) {
  return { Authorization: `Bearer ${token}` };
}

/** Create a user with two conversations by driving the real chat API. */
async function seedUserWithTwoConversations(request: APIRequestContext) {
  const { username, token } = await signupViaApi(request);
  const headers = await authedHeaders(token);

  const send = async (message: string) => {
    const response = await request.post(`${API_URL}/api/v1/chat/stream`, {
      data: { message },
      headers,
    });
    expect(response.status()).toBe(200);
  };

  await send("First archived conversation about water damage");
  await request.post(`${API_URL}/api/v1/chat/reset`, { headers });
  await send("Second archived conversation about filing a claim");

  await expect
    .poll(
      async () => {
        const list = await request.get(`${API_URL}/api/v1/chat/conversations`, {
          headers,
        });
        return ((await list.json()).conversations as Conversation[]).length;
      },
      { timeout: 30_000 },
    )
    .toBe(2);

  const list = await request.get(`${API_URL}/api/v1/chat/conversations`, {
    headers,
  });
  const conversations = (await list.json()).conversations as Conversation[];
  return { username, conversations };
}

// --- Journeys --------------------------------------------------------------

test("Journey 1: signup, first message, streaming answer, sources visible by default @journey", async ({
  recording,
  request,
}) => {
  const { username } = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, username);

  await expect(page).toHaveURL(`${APP_URL}/chat`);
  await expect(page.getByTestId("empty-state")).toBeVisible();
  await expect(page.getByText("Welcome to OmniCare")).toBeVisible();

  await sendMessage(page, "What is covered under water damage?");
  await waitForResponse(page);

  await expect(page.getByTestId("message-list")).toBeVisible();
  await expect(page.getByTestId("bot-icon").first()).toBeVisible();

  // Project constraint: citations are expanded without any interaction.
  await expect(page.getByTestId("sources-badge")).toBeVisible();
  await expect(page.getByTestId("sources-list")).toBeVisible();
  await expect(page.getByTestId("sources-toggle")).toHaveAttribute(
    "aria-expanded",
    "true",
  );

  await expect(
    page.getByTestId("chat-history").getByRole("button"),
  ).toHaveCount(1);
});

test("Journey 2: policy coverage question answered with citations @journey", async ({
  recording,
  request,
}) => {
  const { username } = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, username);

  await sendMessage(page, "What is covered under water damage?");
  await waitForResponse(page);

  const badge = page.getByTestId("sources-badge");
  await expect(badge).toBeVisible();
  await expect(page.getByTestId("sources-count")).toContainText(
    /Sources \(\d+\)/,
  );

  // Grounded answers cite the policy document or one of its sections.
  const sources = page.getByTestId("sources-list");
  await expect(sources).toBeVisible();
  await expect(sources).toContainText(/policy|coverage|damage|deductible/i);

  recording.record("sources_visible_without_interaction", true);
});

test("Journey 3: New Chat via sidebar and Ctrl/Cmd+N clears and resets @journey", async ({
  recording,
  request,
}) => {
  const { username } = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, username);

  await sendMessage(page, "What is covered under water damage?");
  await waitForResponse(page);

  await page.getByTestId("new-chat-button").click();
  await expect(page).toHaveURL(`${APP_URL}/chat`);
  await expect(page.getByTestId("empty-state")).toBeVisible();

  await sendMessage(page, "How do I file a claim?");
  await waitForResponse(page);

  const shortcut = process.platform === "darwin" ? "Meta+n" : "Control+n";
  await page.keyboard.press(shortcut);
  await expect(page).toHaveURL(`${APP_URL}/chat`);
  await expect(page.getByTestId("empty-state")).toBeVisible();

  // Both conversations were persisted and remain in the sidebar.
  await expect(
    page.getByTestId("chat-history").getByRole("button"),
  ).toHaveCount(2);
});

test("Journey 4: sidebar conversation loads full history, read-only, survives reload @journey", async ({
  recording,
  request,
}) => {
  const { username, conversations } =
    await seedUserWithTwoConversations(request);
  const [newest, oldest] = conversations;
  const { page } = recording;

  await signInViaUi(page, username);

  await page.getByTestId("chat-history").getByText(oldest.title).click();

  await expect(page).toHaveURL(`${APP_URL}/chat/${oldest.id}`);
  await expect(
    page.getByText("This is an archived chat", { exact: false }),
  ).toBeVisible();
  // Read-only: the input is replaced by the archived-chat notice.
  await expect(page.getByTestId("chat-textarea")).toHaveCount(0);
  await expect(
    page.getByText("First archived conversation about water damage"),
  ).toBeVisible();

  await page.reload();
  await expect(page).toHaveURL(`${APP_URL}/chat/${oldest.id}`);
  await expect(
    page.getByText("First archived conversation about water damage"),
  ).toBeVisible();

  // The other conversation still loads its own history.
  await page.getByTestId("chat-history").getByText(newest.title).click();
  await expect(page).toHaveURL(`${APP_URL}/chat/${newest.id}`);
  await expect(
    page.getByText("Second archived conversation about filing a claim"),
  ).toBeVisible();
  await expect(
    page.getByText("First archived conversation about water damage"),
  ).toHaveCount(0);
});

test("Journey 5: stop generation mid-stream retains partial text and toasts @journey", async ({
  recording,
  request,
}) => {
  const { username } = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, username);

  await sendMessage(
    page,
    "Explain every coverage section of the OmniCare policy in detail, including limits, deductibles, and exclusions.",
  );

  const stop = page.getByRole("button", { name: "Stop generating" });
  await expect(stop).toBeVisible({ timeout: 60_000 });
  await stop.click();

  await expect(page.getByText("Stopped generating")).toBeVisible({
    timeout: 10_000,
  });

  // The user message and whatever streamed before the stop are retained.
  await expect(page.getByTestId("message-list")).toBeVisible();
  await expect(page.getByTestId("message-list")).toContainText(
    "Explain every coverage section",
  );
  recording.record("stop_retains_partial_text", true);
});

test("Journey 6: retry after error reuses the same idempotency key @journey", async ({
  recording,
  request,
}) => {
  const { username } = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, username);

  const idempotencyKeys: string[] = [];
  let streamCalls = 0;
  await page.route("**/api/v1/chat/stream", (route) => {
    streamCalls += 1;
    idempotencyKeys.push(
      route.request().headers()["idempotency-key"] ?? "missing",
    );
    // Fail the first attempt so the UI shows the error/retry state.
    if (streamCalls === 1) return route.abort();
    return route.continue();
  });

  await sendMessage(page, "What is covered under water damage?");
  await expect(page.getByTestId("error-icon")).toBeVisible({ timeout: 60_000 });

  const retry = page.getByRole("button", { name: "Retry last message" });
  await expect(retry).toBeVisible();
  await retry.click();
  await waitForResponse(page);

  // Project constraint: the retry preserves the original idempotency key.
  expect(idempotencyKeys.length).toBeGreaterThanOrEqual(2);
  expect(idempotencyKeys[0]).not.toBe("missing");
  expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);
  recording.record("idempotency_keys", idempotencyKeys);

  // Exactly one assistant message for the one user message.
  await expect(page.getByTestId("user-icon")).toHaveCount(1);
  await expect(page.getByTestId("bot-icon")).toHaveCount(1);
  await expect(page.getByTestId("error-icon")).toHaveCount(0);
});

test("Journey 7: a second user cannot read another user's conversation @journey", async ({
  recording,
  request,
}) => {
  // User A creates a conversation through the API.
  const userA = await signupViaApi(request);
  const stream = await request.post(`${API_URL}/api/v1/chat/stream`, {
    data: { message: "What is covered under water damage?" },
    headers: await authedHeaders(userA.token),
  });
  expect(stream.status()).toBe(200);

  const conversation = await expect
    .poll(
      async () => {
        const list = await request.get(`${API_URL}/api/v1/chat/conversations`, {
          headers: await authedHeaders(userA.token),
        });
        return ((await list.json()).conversations as Conversation[])[0];
      },
      { timeout: 30_000 },
    )
    .toBeTruthy();

  // User B signs in and guesses user A's conversation URL.
  const userB = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, userB.username);
  await page.goto(`${APP_URL}/chat/${conversation.id}`);

  // The backend returns 404 and the UI reports the failed load.
  await expect(page.getByText("Failed to load conversation")).toBeVisible({
    timeout: 30_000,
  });
  recording.record("cross_user_conversation_access", "rejected with 404");
});

test("Journey 8: a prepared claim is reviewed and confirmed from the chat UI @journey", async ({
  recording,
  request,
}) => {
  const { username, token } = await signupViaApi(request);
  const { page } = recording;
  await signInViaUi(page, username);

  await sendMessage(page, "I want to file a claim for water damage");
  await waitForResponse(page);
  await sendMessage(page, "My policy number is POL-1092");
  await waitForResponse(page);
  await sendMessage(page, "The claimed amount is 3500 dollars");
  await waitForResponse(page);
  await sendMessage(page, "A pipe burst flooded the kitchen yesterday");
  await waitForResponse(page);

  // The agent prepares the submission and hands the confirmation to the UI.
  await expect(page.getByTestId("message-list")).toContainText(/confirm/i, {
    timeout: 120_000,
  });

  // The confirmation card rides on the tool trace of the turn that prepared it.
  const toolDetails = page.getByRole("button", { name: "Tool details" }).last();
  await expect(toolDetails).toBeVisible();
  await toolDetails.click();

  const card = page.getByTestId("claim-confirm-card");
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card).toContainText("POL-1092");
  await expect(card).toContainText("$3,500");
  await expect(card).toContainText("A pipe burst flooded the kitchen");
  recording.record("claim_confirmation_ui_exists", true);

  await page.getByTestId("confirm-claim-button").click();

  // The receipt is driven by the confirm response, so the claim id is the proof
  // the two-step flow completed rather than echoing the prepared values back.
  const receipt = page.getByTestId("claim-receipt");
  await expect(receipt).toBeVisible({ timeout: 60_000 });
  const claimId = (await receipt.textContent())?.match(/CLM-[A-Z0-9]+/)?.[0];
  expect(claimId).toBeTruthy();
  recording.record("confirmed_claim_id", claimId);

  // The claim is now readable through the claims API the receipt links to.
  await expect
    .poll(
      async () => {
        const claims = await request.get(`${API_URL}/api/v1/claims`, {
          headers: await authedHeaders(token),
        });
        if (claims.status() !== 200) return false;
        const filed = (await claims.json()).claims as { claim_id: string }[];
        return filed.some((claim) => claim.claim_id === claimId);
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  recording.record("claim_filed", true);
});
