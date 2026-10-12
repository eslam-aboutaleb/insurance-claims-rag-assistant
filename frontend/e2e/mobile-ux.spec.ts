/**
 * Phone-viewport behaviour of the chat shell (T15 layout work, T17 touch
 * affordances).
 *
 * Stack requirement: the full stack must be up -- frontend on
 * http://localhost:3000, backend on http://localhost:8000 -- and the frontend
 * bundle must be the one built from the current source tree. A container built
 * before those tracks landed serves markup without the mobile padding, the
 * inert wiring or the touch-visible action row, so every assertion here would
 * fail against a stale bundle.
 *
 * The file runs under Playwright's Pixel 5 device (393x851, touch, no hover).
 * One nested describe switches back to a desktop viewport so the same markup
 * can be shown to be neither inert nor permanently-on-top there.
 */

import { test, expect, devices } from "@playwright/test";
import {
  APP_URL,
  SECOND_CONVERSATION_MESSAGE,
  findByTitle,
  gotoWithSession,
  seedUserWithTwoConversations,
  signIn,
  signUpViaApi,
} from "./helpers";

// The clipboard permission is what lets a tap on "Copy message" actually write,
// which is the only way a phone has of reaching a control that never receives a
// hover.
test.use({ ...devices["Pixel 5"], permissions: ["clipboard-read", "clipboard-write"] });

/** The action row an assistant answer carries in its top-right corner. */
const actionRow = '[data-testid^="copy-button-"], [data-testid^="feedback-up-"], [data-testid^="feedback-down-"]';

test.describe("phone viewport", () => {
  test("message actions are reachable without hover and the transcript clears the hamburger", async ({
    page,
    request,
  }) => {
    const { username, token, conversations } =
      await seedUserWithTwoConversations(request);
    const second = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);

    // A deep link rather than the drawer on purpose: this test must never move
    // the pointer, because "no hover" is exactly what is being asserted.
    // Playwright treats an opacity:0 element as visible, so the assertion that
    // carries the weight is the computed opacity of each control.
    await signIn(page, username);
    await gotoWithSession(page, `${APP_URL}/chat/${second.id}`, token);

    await expect(page.getByTestId("message-list")).toBeVisible();
    await expect(
      page.getByTestId("message-list").getByText(SECOND_CONVERSATION_MESSAGE),
    ).toBeVisible();

    const copy = page.locator('[data-testid^="copy-button-"]').first();
    const thumbsUp = page.locator('[data-testid^="feedback-up-"]').first();
    const thumbsDown = page.locator('[data-testid^="feedback-down-"]').first();

    for (const control of [copy, thumbsUp, thumbsDown]) {
      await expect(control).toBeVisible();
      await expect(control).toHaveCSS("opacity", "1");
      await expect(control).toBeEnabled();
    }

    // A tap is what a phone has instead of a hover, so the copy control answers
    // one directly rather than waiting for a pointer that never arrives.
    await copy.tap();
    await expect(page.getByLabel("Copied")).toBeVisible();

    // The floating hamburger overlaps the top-left of the transcript area, so
    // the mobile padding the scroll container carries is what keeps the first
    // turn out from under it.
    const messagesContainer = page.getByTestId("messages-container");
    const containerClass = (await messagesContainer.getAttribute("class")) ?? "";
    expect(containerClass).toContain("pl-16");
    expect(containerClass).toContain("pt-16");

    const hamburger = page.getByTestId("mobile-menu-toggle");
    const hamburgerBox = await hamburger.boundingBox();
    const firstElement = page
      .locator('[data-testid="message-list"] > div')
      .first();
    const firstBox = await firstElement.boundingBox();

    expect(hamburgerBox).not.toBeNull();
    expect(firstBox).not.toBeNull();
    expect(firstBox!.y).toBeGreaterThanOrEqual(hamburgerBox!.y + hamburgerBox!.height);
  });

  test("the hamburger opens the drawer and the closed sidebar stays out of the tab order", async ({
    page,
    request,
  }) => {
    const { username } = await signUpViaApi(request);
    await signIn(page, username);

    const sidebar = page.getByTestId("sidebar-container");
    const chat = page.getByTestId("chat-container");
    const toggle = page.getByTestId("mobile-menu-toggle");

    // Off-canvas means out of the accessibility tree, not merely off-screen.
    await expect(toggle).toBeVisible();
    await expect(sidebar).toHaveAttribute("inert", "");
    await expect(sidebar).toHaveAttribute("aria-hidden", "true");
    await expect(chat).not.toHaveAttribute("inert", "");

    // Walking the tab order from the app root must never reach the hidden rail.
    await toggle.focus();
    for (let step = 0; step < 20; step += 1) {
      await page.keyboard.press("Tab");
      const reachedSidebar = await page.evaluate(
        () =>
          document.activeElement?.closest(
            '[data-testid="sidebar-container"]',
          ) !== null,
      );
      expect(reachedSidebar, `tab stop ${step + 1} entered the hidden sidebar`).toBe(
        false,
      );
    }

    // Opening the drawer inverts it: the transcript is what is inert now.
    await toggle.click();
    await expect(page.getByTestId("sidebar-overlay")).toBeVisible();
    await expect(sidebar).not.toHaveAttribute("inert", "");
    await expect(chat).toHaveAttribute("inert", "");
    await expect(chat).toHaveAttribute("aria-hidden", "true");

    // Dismissing the drawer gives the transcript back.
    await page.getByTestId("sidebar-overlay").click();
    await expect(sidebar).toHaveAttribute("inert", "");
    await expect(chat).not.toHaveAttribute("inert", "");
  });
});

test.describe("desktop viewport", () => {
  // A desktop-width, no-touch context on the same page. Only the device's
  // layout-relevant fields are overridden: `defaultBrowserType` is not allowed
  // inside a describe group because it forces a new worker.
  test.use({
    viewport: { width: 1280, height: 720 },
    hasTouch: false,
    isMobile: false,
    deviceScaleFactor: 1,
  });

  test("the desktop shell is never inert and message actions stay hover-revealed", async ({
    page,
    request,
  }) => {
    const { username, token, conversations } =
      await seedUserWithTwoConversations(request);
    const second = findByTitle(conversations, SECOND_CONVERSATION_MESSAGE);
    await signIn(page, username);
    await gotoWithSession(page, `${APP_URL}/chat/${second.id}`, token);

    await expect(page.getByTestId("message-list")).toBeVisible();

    // The rail is part of the desktop layout, so neither half of the page is
    // taken out of the accessibility tree here.
    await expect(page.getByTestId("sidebar-container")).not.toHaveAttribute(
      "inert",
      "",
    );
    await expect(page.getByTestId("chat-container")).not.toHaveAttribute(
      "inert",
      "",
    );
    await expect(page.getByTestId("mobile-menu-toggle")).toBeHidden();

    // Above the sm breakpoint the reveal rule is back, so the row is not left
    // permanently on top of the answer it belongs to.
    const copy = page.locator('[data-testid^="copy-button-"]').first();
    await expect(copy).toHaveCSS("opacity", "0");
    await copy.hover();
    await expect(copy).toHaveCSS("opacity", "1");
  });
});
