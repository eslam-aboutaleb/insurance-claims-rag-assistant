/**
 * Session-expiry behaviour (T14 wording work, T15 wiring).
 *
 * Stack requirement: the full stack must be up -- frontend on
 * http://localhost:3000, backend on http://localhost:8000. The stream is
 * intercepted, so no LLM call is made by this spec: a rejected session is
 * answered by the auth layer before the agent ever runs, which is exactly the
 * path a 401 takes.
 */

import { test, expect } from "@playwright/test";
import { APP_URL, signIn, signUpViaApi } from "./helpers";

test("a 401 mid-answer ends the session and says the session expired", async ({
  page,
  request,
}) => {
  const { username } = await signUpViaApi(request);
  await signIn(page, username);

  await expect(page.getByTestId("chat-textarea")).toBeVisible();

  // The backend answers an unauthenticated stream with a 401 before the first
  // frame, so this is the shape of the real failure rather than a contrivance.
  await page.route("**/api/v1/chat/stream", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Not authenticated" }),
    }),
  );

  await page
    .getByTestId("chat-textarea")
    .fill("What is covered under water damage?");
  await page.getByTestId("send-button").click();

  // Read first: the toast is transient, the rest of the flow is not.
  await expect(
    page.getByText("Session expired. Please sign in again."),
  ).toBeVisible();
  // The deliberate-sign-out wording must not be borrowed for an expiry.
  await expect(page.getByText("Logged out successfully")).toHaveCount(0);

  await expect(page).toHaveURL(`${APP_URL}/login`);
  await expect(
    page.getByRole("heading", { name: "Sign in to OmniCare" }),
  ).toBeVisible();

  // The session is genuinely gone rather than merely navigated away from.
  const storedToken = await page.evaluate(() =>
    window.localStorage.getItem("omnicare_token"),
  );
  expect(storedToken).toBeNull();
});
