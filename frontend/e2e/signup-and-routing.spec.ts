/**
 * Sign-up auto-login, unknown-route redirects and auth form semantics (T14).
 *
 * Stack requirement: the full stack must be up -- frontend on
 * http://localhost:3000, backend on http://localhost:8000. Sign-up drives the
 * real auth API, so a fresh account is created per test and the assertions
 * below are about what the app does with the session it was handed.
 */

import { test, expect } from "@playwright/test";
import {
  APP_URL,
  PASSWORD,
  recordedNavigations,
  recordHistoryNavigations,
  signIn,
  signUpViaApi,
  uniqueEmail,
} from "./helpers";

test.describe("sign-up", () => {
  test("signing up through the UI lands on /chat authenticated, with no stop at /login", async ({
    page,
  }) => {
    const username = uniqueEmail("signup");
    await recordHistoryNavigations(page);

    await page.goto(`${APP_URL}/signup`);
    await expect(
      page.getByRole("heading", { name: "Create your OmniCare account" }),
    ).toBeVisible();

    await page.getByLabel("Email", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Confirm password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(page).toHaveURL(`${APP_URL}/chat`);

    // The token sign-up returned is adopted, so the app is usable straight
    // away: a real chat shell, not an auth page waiting for a second sign-in.
    await expect(page.getByTestId("chat-window")).toBeVisible();
    await expect(page.getByTestId("chat-textarea")).toBeVisible();
    const storedToken = await page.evaluate(() =>
      window.localStorage.getItem("omnicare_token"),
    );
    expect(storedToken).toBeTruthy();

    // A client-side redirect is invisible to a URL snapshot, so the History API
    // trail is what proves the login page was never visited at all.
    const navigations = await recordedNavigations(page);
    expect(
      navigations.filter((url) => url.includes("/login")),
      `navigations: ${navigations.join(" -> ")}`,
    ).toEqual([]);
  });
});

test.describe("unknown routes", () => {
  test("an unknown path sends a signed-out visitor to the login page", async ({
    page,
  }) => {
    await page.goto(`${APP_URL}/definitely-not-a-route`);
    await expect(page).toHaveURL(`${APP_URL}/login`);
    await expect(
      page.getByRole("heading", { name: "Sign in to OmniCare" }),
    ).toBeVisible();
  });

  test("an unknown path sends a signed-in visitor to the chat", async ({
    page,
    request,
  }) => {
    const { username } = await signUpViaApi(request);
    await signIn(page, username);

    await page.goto(`${APP_URL}/definitely-not-a-route`);
    await expect(page).toHaveURL(`${APP_URL}/chat`);
    await expect(page.getByTestId("chat-window")).toBeVisible();
  });
});

test.describe("auth form semantics", () => {
  test("the login form's inputs resolve by their labels", async ({ page }) => {
    await page.goto(`${APP_URL}/login`);

    // Each label is associated with its input, so an accessible-name lookup is
    // the reliable way to reach the fields. "Password" is matched exactly: the
    // show/hide toggle inside the field is announced as "Show password", which
    // a substring lookup would also resolve to.
    const email = page.getByLabel("Email");
    const password = page.getByLabel("Password", { exact: true });
    await expect(email).toBeVisible();
    await expect(password).toBeVisible();
    await expect(email).toHaveAttribute("id", "auth-email");
    await expect(password).toHaveAttribute("id", "auth-password");
    await expect(email).toBeFocused();
  });

  test("a blank submit is stopped by the browser before any request", async ({
    page,
  }) => {
    const signInCalls: string[] = [];
    await page.route("**/api/v1/auth/signin", (route) => {
      signInCalls.push(route.request().url());
      return route.continue();
    });

    await page.goto(`${APP_URL}/login`);
    await page.getByRole("button", { name: "Sign in" }).click();

    // The inputs carry `required` and `type="email"`, so constraint validation
    // guards an empty submit before the handler runs.
    await expect(page.getByText("Email is required.")).toHaveCount(0);
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    expect(signInCalls).toEqual([]);
  });

  test("an invalid submit exposes the error on the input it belongs to", async ({
    page,
  }) => {
    await page.goto(`${APP_URL}/login`);

    // Constraint validation is off so the form's own checks are what answers
    // the submit, the way they would in a browser that does not enforce them.
    await page.locator("form").first().evaluate((form: HTMLFormElement) => {
      form.noValidate = true;
    });
    await page.getByRole("button", { name: "Sign in" }).click();

    const email = page.getByLabel("Email", { exact: true });
    await expect(page.getByText("Email is required.")).toBeVisible();
    await expect(email).toHaveAttribute("aria-invalid", "true");
    await expect(email).toHaveAttribute("aria-describedby", "auth-email-error");

    // The field the attribute points at is the field carrying the message, and
    // the empty submit is reported on both fields rather than stopping at the
    // first one.
    const describedBy = await email.getAttribute("aria-describedby");
    await expect(page.locator(`#${describedBy}`)).toHaveText("Email is required.");
    const passwordField = page.getByLabel("Password", { exact: true });
    await expect(passwordField).toHaveAttribute("aria-invalid", "true");
    await expect(passwordField).toHaveAttribute(
      "aria-describedby",
      "auth-password-error",
    );
    await expect(page.locator("#auth-password-error")).toHaveText(
      "Password is required.",
    );

    // A malformed address is reported the same way.
    await email.fill("someone@localhost");
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Enter a valid email address.")).toBeVisible();
    await expect(email).toHaveAttribute("aria-invalid", "true");

    // And so is a password that is too short, on its own input.
    await email.fill("someone@example.com");
    await page.getByLabel("Password", { exact: true }).fill("123");
    await page.getByRole("button", { name: "Sign in" }).click();
    const password = page.getByLabel("Password", { exact: true });
    await expect(
      page.getByText("Password must be at least 6 characters."),
    ).toBeVisible();
    await expect(password).toHaveAttribute("aria-invalid", "true");
    await expect(password).toHaveAttribute(
      "aria-describedby",
      "auth-password-error",
    );
  });

  test("the sign-up form reports a password mismatch on its own input", async ({
    page,
  }) => {
    await page.goto(`${APP_URL}/signup`);
    await page.getByLabel("Email", { exact: true }).fill(uniqueEmail("mismatch"));
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Confirm password", { exact: true }).fill("Different123!");
    await page.getByRole("button", { name: "Create account" }).click();

    const confirm = page.getByLabel("Confirm password", { exact: true });
    await expect(page.getByText("Passwords do not match.")).toBeVisible();
    await expect(confirm).toHaveAttribute("aria-invalid", "true");
    await expect(confirm).toHaveAttribute(
      "aria-describedby",
      "auth-confirm-password-error",
    );
    // The mismatch is a client-side refusal, so no account was created.
    await expect(page).toHaveURL(`${APP_URL}/signup`);
  });
});
