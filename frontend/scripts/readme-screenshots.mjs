/**
 * Capture the real UI using synthetic API fixtures (no backend or LLM needed).
 * Start `npm run dev`, then run `node scripts/readme-screenshots.mjs`.
 * Set SCREENSHOT_BASE_URL to capture another local frontend instance.
 */
import { chromium, expect } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";

const baseURL = process.env.SCREENSHOT_BASE_URL || "http://127.0.0.1:3000";
const output = fileURLToPath(new URL("../public/readme/", import.meta.url));
const userId = "11111111-1111-4111-8111-111111111111";
const timestamp = "2026-10-28T11:00:00Z";
const coverageQuestion = "What is covered under water damage?";
const coverageAnswer = "Your home policy covers **sudden and accidental water damage**, including a burst pipe.\n\n- **Coverage limit:** $25,000 per incident\n- **Deductible:** $500\n- **Exclusions:** gradual leaks, lack of maintenance, and flooding\n\nSee Section 1 of your policy for the full terms and exclusions.";
const claimAnswer = "Claim **CLM-8821** is **under review**. Your $3,200 water damage claim has been received and is being assessed.\n\nYou can check the claim details below.";
const sources = ["Home Insurance Policy — Section 1: Water Damage Coverage"];
const coverageResult = {
  coverage_limit: 25000,
  deductible: 500,
  exclusions: ["Gradual leaks", "Flooding"],
};
const claimResult = {
  found: true,
  claim_id: "CLM-8821",
  policy_number: "POL-HOME-2048",
  claim_type: "Water Damage",
  status: "Under Review",
  amount: 3200,
  citation: "Claims record: CLM-8821",
};
const conversations = [coverageQuestion, "What does my policy cover?", "Help me file a water damage claim"].map((title, i) => ({
  id: `22222222-2222-4222-8222-${String(i + 1).padStart(12, "0")}`,
  title,
  created_at: timestamp,
  updated_at: timestamp,
}));

await mkdir(output, { recursive: true });
const browser = await chromium.launch();
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
    colorScheme: "light",
    reducedMotion: "reduce",
    locale: "en-US",
    timezoneId: "Africa/Cairo",
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.clock.setFixedTime(new Date("2026-10-28T12:00:00Z"));
  let authenticated = false;
  let showConversations = false;
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") {
      return route.fulfill({ status: authenticated ? 200 : 401, json: authenticated ? { user_id: userId } : { detail: "Not authenticated" } });
    }
    if (path === "/api/v1/chat/conversations") {
      return route.fulfill({ json: { conversations: showConversations ? conversations : [] } });
    }
    if (path === "/api/v1/chat/stream") {
      const { message } = route.request().postDataJSON();
      const claim = message.includes("CLM-8821");
      const tool = claim ? "get_claim_status" : "query_policy";
      const events = [
        { type: "tool_call", tool, arguments: claim ? { claim_id: "CLM-8821" } : { query: message } },
        { type: "tool_result", tool, result: claim ? claimResult : coverageResult },
        { type: "text_delta", content: claim ? claimAnswer : coverageAnswer },
        { type: "response_complete", sources: claim ? [claimResult.citation] : sources, status: "ok" },
      ];
      return route.fulfill({ contentType: "text/event-stream", body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join("") });
    }
    throw new Error(`Unexpected API request while capturing screenshots: ${path}`);
  });

  async function capture(name) {
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${output}${name}`, animations: "disabled" });
    console.log(`Captured ${name}`);
  }
  async function openChat() {
    await page.goto(`${baseURL}/chat`);
    await expect(page.getByRole("heading", { name: "Welcome to OmniCare" })).toBeVisible();
    await expect(page.getByTestId("chat-textarea")).toBeVisible();
  }
  async function send(message, answer) {
    await page.getByTestId("chat-textarea").fill(message);
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page.getByText(answer, { exact: false })).toBeVisible();
    await expect(page.getByTestId("sources-badge")).toBeVisible();
    await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0);
    await page.getByTestId("chat-textarea").blur();
  }

  await page.goto(`${baseURL}/login`);
  await expect(page.getByRole("heading", { name: "Sign in to OmniCare" })).toBeVisible();
  await page.getByLabel("Email", { exact: true }).blur();
  await capture("02-login.png");

  authenticated = true;
  await page.addInitScript(id => {
    localStorage.setItem("omnicare_token", "demo.screenshot.session");
    localStorage.setItem("omnicare_user_id", id);
  }, userId);
  await openChat();
  await capture("01-empty-state.png");

  showConversations = true;
  await openChat();
  await expect(page.getByText("What does my policy cover?", { exact: true })).toBeVisible();
  await capture("03-chat-interface.png");
  await send(coverageQuestion, "Your home policy covers");
  await page.getByTestId("sources-toggle").click();
  await capture("04-policy-question.png");

  await page.getByRole("button", { name: "Tool details" }).click();
  await page.locator("summary").filter({ hasText: "query_policy" }).click();
  await capture("05-tool-details.png");

  await openChat();
  await send("What is the status of claim CLM-8821?", "Your $3,200 water damage claim");
  const details = page.getByRole("button", { name: "Tool details" });
  if (await details.getAttribute("aria-expanded") !== "true") await details.click();
  await expect(page.getByTestId("claim-status-pill")).toBeVisible();
  await capture("06-claim-status.png");
  if (errors.length) throw new Error(errors.join("\n"));
} finally {
  await browser.close();
}
