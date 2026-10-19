/**
 * Shared helpers for the OmniCare end-to-end specs.
 *
 * Stack requirement: every spec in this directory needs the full stack up --
 * frontend on http://localhost:3000 and backend on http://localhost:8000 --
 * because they seed real conversations through the chat API and sign in
 * through the login form. `playwright.config.ts` runs
 * `docker compose up --build frontend` and reuses whatever is already
 * listening on :3000, so a rebuild is required whenever the bundle under test
 * must match the current source tree.
 *
 * This directory is deliberately outside `tsconfig.json` (`include: ["src"]`),
 * so these helpers are only checked by Playwright's own transpile step.
 */

import { execFileSync } from "node:child_process";
import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const API_URL = "http://localhost:8000";
export const APP_URL = "http://localhost:3000";
export const PASSWORD = "TestPass123!";

export type Conversation = { id: string; title: string };

export type SeededUser = {
  username: string;
  token: string;
  conversations: Conversation[];
};

/** The message the first seeded conversation is created from. */
export const FIRST_CONVERSATION_MESSAGE =
  "First archived conversation about water damage";
/** The message the second seeded conversation is created from. */
export const SECOND_CONVERSATION_MESSAGE =
  "Second archived conversation about filing a claim";

/** Looks a seeded conversation up by the message it was created from. */
export function findByTitle(
  conversations: Conversation[],
  title: string,
): Conversation {
  const found = conversations.find((conversation) => conversation.title === title);
  if (!found) {
    throw new Error(
      `No conversation titled "${title}" in ${JSON.stringify(conversations)}`,
    );
  }
  return found;
}

/** A unique address per test, so parallel workers never share a session. */
export function uniqueEmail(prefix: string): string {
  return `${prefix}_${Date.now()}${Math.floor(Math.random() * 1e6)}@example.com`;
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/**
 * Registers a fresh account through the auth API and reports its session.
 *
 * Signup auto-authenticates server-side, so the returned token is immediately
 * usable for seeding and for the assertions that need to read the server's own
 * view of a conversation.
 */
export async function signUpViaApi(
  request: APIRequestContext,
): Promise<{ username: string; token: string; userId: string }> {
  const username = uniqueEmail("e2e");
  const response = await request.post(`${API_URL}/api/v1/auth/signup`, {
    data: { username, password: PASSWORD },
  });
  expect(response.status()).toBe(201);
  const body = await response.json();
  return {
    username,
    token: body.access_token as string,
    userId: body.user_id as string,
  };
}

/** Reads the caller's conversation list straight from the API. */
export async function listConversations(
  request: APIRequestContext,
  token: string,
): Promise<Conversation[]> {
  const response = await request.get(`${API_URL}/api/v1/chat/conversations`, {
    headers: authHeaders(token),
  });
  expect(response.status()).toBe(200);
  const body = await response.json();
  return body.conversations as Conversation[];
}

/**
 * Creates a user with two conversations by driving the real chat API, so the
 * browser assertions run against genuine backend data.
 *
 * Each response body is read to completion: a turn is persisted only once its
 * stream closes, and resolving on the response headers alone leaves the second
 * stream overlapping the first, which makes the order the list comes back in
 * (``updated_at`` descending) a coin toss. The caller addresses conversations
 * by title rather than by position for the same reason.
 */
export async function seedUserWithTwoConversations(
  request: APIRequestContext,
): Promise<SeededUser> {
  const { username, token } = await signUpViaApi(request);
  const headers = authHeaders(token);

  const send = async (message: string) => {
    const response = await request.post(`${API_URL}/api/v1/chat/stream`, {
      data: { message },
      headers,
    });
    expect(response.status()).toBe(200);
    // Reading the body to the end is what guarantees the turn was persisted
    // before the next one starts.
    await response.text();
  };

  await send(FIRST_CONVERSATION_MESSAGE);
  await request.post(`${API_URL}/api/v1/chat/reset`, { headers });
  await send(SECOND_CONVERSATION_MESSAGE);

  await expect
    .poll(async () => (await listConversations(request, token)).length, {
      timeout: 30_000,
    })
    .toBe(2);

  return { username, token, conversations: await listConversations(request, token) };
}

/**
 * Signs in through the login form, so the auth surface under test is the real
 * one rather than a seeded token.
 *
 * The waits after the redirect are what make a following full page load safe.
 * The token is written to localStorage during sign-in and committed by the
 * browser asynchronously, so a navigation issued in the same turn as the
 * redirect can otherwise land in a cookie-only session: authenticated through
 * the session cookie, with no bearer token in React state, where every request
 * that needs the ``Authorization`` header stays disabled and the sidebar reads
 * "No conversations yet". Waiting for the token to be readable and for the
 * authenticated shell to commit keeps that race out of the specs that follow.
 */
export async function signIn(page: Page, username: string): Promise<void> {
  await page.goto(`${APP_URL}/login`);
  await page.getByLabel("Email", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL("**/chat", { timeout: 30_000 });

  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const stored = window.localStorage.getItem("omnicare_token");
          return typeof stored === "string" && stored.includes(".");
        }),
      { timeout: 15_000 },
    )
    .toBe(true);

  // The shell is only mounted once the session has been revalidated, so this
  // both proves the session works and gives the token write time to commit.
  await expect(page.getByTestId("sidebar-container")).toBeVisible();
  await expect(page.getByTestId("chat-container")).toBeVisible();
}

/** Candidate locations for the docker CLI; it is not always on PATH. */
const DOCKER_CANDIDATES = ["docker", "/opt/homebrew/bin/docker", "/usr/local/bin/docker"];

/** The backend container every seed runs inside. */
const BACKEND_CONTAINER = "omnicare-backend";

/**
 * Locates a usable docker CLI, or null when this host has none.
 *
 * The search order is the PATH first and the well-known install locations
 * second, because the toolchain that runs these specs frequently has a
 * deliberately minimal PATH.
 */
export function dockerBinary(): string | null {
  for (const candidate of DOCKER_CANDIDATES) {
    try {
      execFileSync(candidate, ["--version"], { stdio: "ignore" });
      return candidate;
    } catch {
      // Not usable here; try the next candidate.
    }
  }
  return null;
}

/**
 * Inserts a conversation row that owns no messages.
 *
 * The chat API only creates a conversation alongside a persisted turn, so the
 * "no messages yet" state cannot be reached through the endpoints. This script
 * writes the row with the application's own session factory and models, inside
 * the backend container, rather than hand-writing SQL against the database.
 */
const EMPTY_CONVERSATION_SEED_SCRIPT = `
import asyncio
import os
import uuid

from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.conversations import Conversation


async def main() -> None:
    user_id = uuid.UUID(os.environ["SEED_USER_ID"])
    session_id = os.environ["SEED_SESSION_ID"]
    title = os.environ["SEED_TITLE"]
    async with async_session_factory() as session:
        existing = (
            await session.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(existing.id)
            return
        conversation = Conversation(
            user_id=user_id,
            session_id=session_id,
            title=title,
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        print(conversation.id)


asyncio.run(main())
`;

/**
 * Seeds a user whose only conversation holds no messages.
 *
 * Returns null when no docker CLI is available, which is the only legitimate
 * reason this fixture cannot be built; the caller skips the test in that case
 * rather than reporting a product failure.
 */
export async function seedEmptyConversation(
  request: APIRequestContext,
): Promise<{ username: string; token: string; conversationId: string } | null> {
  const docker = dockerBinary();
  if (!docker) return null;

  const { username, token, userId } = await signUpViaApi(request);
  const sessionId = `e2e-empty-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

  const stdout = execFileSync(
    docker,
    [
      "exec",
      "-i",
      "-e",
      `SEED_USER_ID=${userId}`,
      "-e",
      `SEED_SESSION_ID=${sessionId}`,
      "-e",
      "SEED_TITLE=Empty archived conversation",
      BACKEND_CONTAINER,
      "python3",
      "-",
    ],
    {
      input: EMPTY_CONVERSATION_SEED_SCRIPT,
      encoding: "utf8",
      timeout: 60_000,
    },
  );

  const conversationId = stdout.trim().split("\n").pop()?.trim();
  if (!conversationId) {
    throw new Error(`Seeding an empty conversation produced no id: ${stdout}`);
  }

  // The browser only ever reads that row through the API, so the fixture is not
  // finished until the endpoint reports it back with no messages.
  const detail = await request.get(
    `${API_URL}/api/v1/chat/conversations/${conversationId}`,
    { headers: authHeaders(token) },
  );
  expect(detail.status()).toBe(200);
  const body = await detail.json();
  expect(body.messages as unknown[]).toEqual([]);

  return { username, token, conversationId };
}

/**
 * Records every History API navigation the app performs from here on.
 *
 * A client-side redirect is invisible to `page.url()` snapshots, so a spec that
 * has to prove a route was never visited needs its own trail. Must be called
 * before the first `page.goto` of the flow it observes.
 */
export async function recordHistoryNavigations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const collected: string[] = [window.location.href];
    (window as unknown as { __e2eNavigations: string[] }).__e2eNavigations =
      collected;
    for (const method of ["pushState", "replaceState"] as const) {
      const original = history[method].bind(history);
      history[method] = ((...args: unknown[]) => {
        const result = (original as (...a: unknown[]) => unknown)(...args);
        collected.push(window.location.href);
        return result;
      }) as History["pushState"];
    }
  });
}

/** Reads back the trail `recordHistoryNavigations` started collecting. */
export async function recordedNavigations(page: Page): Promise<string[]> {
  return page.evaluate(
    () =>
      (window as unknown as { __e2eNavigations?: string[] })
        .__e2eNavigations ?? [],
  );
}

/**
 * Follows a full page load with a session the new document is guaranteed to
 * read.
 *
 * A sign-in writes the token to localStorage and the browser commits that write
 * asynchronously. A navigation issued in the same turn as the redirect can
 * therefore tear the document down before the commit lands, leaving the next
 * document with no bearer token: authenticated only through the session cookie,
 * with the bearer token absent from React state, so every request that needs the
 * ``Authorization`` header stays disabled and the sidebar reports no
 * conversations at all. Re-asserting the token for the document that is about to
 * load removes that race from the spec without changing anything the app has to
 * do -- the token is still validated against ``/api/v1/auth/me`` on hydration.
 *
 * Only for specs that reload the page after signing in; a client-side
 * navigation carries the in-memory session and needs none of this.
 */
export async function gotoWithSession(
  page: Page,
  url: string,
  token: string,
): Promise<void> {
  await page.addInitScript((value: string) => {
    window.localStorage.setItem("omnicare_token", value);
  }, token);
  await page.goto(url);
}

/** The suggestion chips the new-chat empty state offers. */
export const WELCOME_CHIP_LABELS = [
  "Check my recent claims",
  "What is covered under water damage?",
  "File a new claim",
] as const;
