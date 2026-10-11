import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import MessageBubble from "@/components/MessageBubble";
import { Message, ToolCall } from "@/types/chat";
import { toPendingClaimConfirmation } from "@/lib/api";

// The claim confirmation card reads the session token through this hook, and a
// bare render of the bubble has no provider to read it from.
vi.mock("@/context/AuthContext", () => ({
  useAuth: () => ({
    token: "session-token-abc",
    userId: "user-123",
    status: "authenticated",
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

function assistantMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    role: "assistant",
    content: "Water damage is covered for sudden events.",
    timestamp: new Date("2024-01-02T10:30:00Z"),
    ...overrides,
  };
}

const COPY_LABEL = "Copy message";

/** A citation with no excerpt, so the pill renders as plain text. */
const CITATION = "Section 1: Home Water Damage Coverage (sample_policy.md)";

/** The invocation that stages a claim, as the hook stores it on the turn. */
function preparedClaimToolCall(): ToolCall {
  const call: ToolCall = {
    name: "prepare_claim_submission",
    arguments: {
      policy_number: "POL-1092",
      claim_type: "Water Damage",
      amount: 3500,
      description: "A pipe burst flooded the kitchen yesterday",
    },
  };
  return {
    ...call,
    // Built through the same guard the disclosure reads, so this fixture
    // cannot quietly drift from the shape the component checks for.
    result: toPendingClaimConfirmation(call, {
      confirmation_token: "8f3c2a54-1c1e-4f6a-9d3b-7b2c1a4e5f60",
      expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
      status: "pending",
      message: "Your claim has been prepared.",
    }),
  };
}

describe("MessageBubble", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the copy button in the tab order and reveals it on focus", () => {
    render(<MessageBubble message={assistantMessage()} />);

    const button = screen.getByRole("button", { name: COPY_LABEL });
    expect(button).toHaveAttribute("type", "button");
    // A real button with no tabIndex override is reachable by Tab, so pressing
    // it has to make it visible rather than leaving it at opacity-0.
    expect(button).not.toHaveAttribute("tabindex");
    button.focus();
    expect(document.activeElement).toBe(button);
    // Touch reachability: the row is permanently visible below the ``sm``
    // breakpoint, where there is no hover to reveal it with, and only falls
    // back to the hover reveal on a pointer-equipped desktop.
    expect(button.className).toContain("sm:group-hover:sm:opacity-100");
    expect(button.className).toContain("sm:focus-visible:sm:opacity-100");
    expect(button.className).toContain("focus-visible:opacity-100");
  });

  it("copies the message text and confirms with the accessible label", async () => {
    render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: COPY_LABEL }));
    });

    expect(writeText).toHaveBeenCalledWith(
      "Water damage is covered for sudden events.",
    );
    expect(
      await screen.findByRole("button", { name: "Copied" }),
    ).toBeInTheDocument();
  });

  it("clears the pending copy confirmation when the bubble unmounts", async () => {
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    const view = render(<MessageBubble message={assistantMessage()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: COPY_LABEL }));
    });
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();

    clearTimeoutSpy.mockClear();
    view.unmount();

    // The timer that reverts the label is cancelled with the component rather
    // than left to fire into a bubble that is no longer on screen.
    expect(clearTimeoutSpy).toHaveBeenCalledTimes(1);
  });

  it("opens the tool details itself when a claim is waiting on a confirmation", () => {
    render(
      <MessageBubble
        message={assistantMessage({ toolCalls: [preparedClaimToolCall()] })}
      />,
    );

    // The confirmation card is the only way a prepared claim becomes a filed
    // one, so it is on screen without the user having to open anything.
    const card = screen.getByTestId("claim-confirm-card");
    expect(card).toBeInTheDocument();
    expect(card).toBeVisible();
    expect(card).toHaveTextContent("$3,500");
    expect(screen.getByTestId("tool-details-toggle-m1")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("never hides a claim that is still waiting on a decision", () => {
    render(
      <MessageBubble
        message={assistantMessage({ toolCalls: [preparedClaimToolCall()] })}
      />,
    );

    // Collapsing the disclosure would put the confirm button back behind a
    // control that reads like debug output.
    fireEvent.click(screen.getByTestId("tool-details-toggle-m1"));

    expect(screen.getByTestId("claim-confirm-card")).toBeVisible();
  });

  it("keeps the tool details closed until the user asks for an ordinary trace", () => {
    render(
      <MessageBubble
        message={assistantMessage({
          toolCalls: [
            { name: "query_policy", arguments: { query: "water damage" } },
          ],
        })}
      />,
    );

    const toggle = screen.getByTestId("tool-details-toggle-m1");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByTestId("tool-calls-container-m1"),
    ).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.getByTestId("tool-calls-container-m1")).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(toggle);
    expect(
      screen.queryByTestId("tool-calls-container-m1"),
    ).not.toBeInTheDocument();
  });

  it("keeps the partial answer and appends the failure beneath it", () => {
    render(
      <MessageBubble
        message={assistantMessage({
          content: "Water damage is covered",
          error: "The answer could not be completed.",
        })}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByTestId("assistant-text-m1")).toHaveTextContent(
      "Water damage is covered",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The answer could not be completed.",
    );
  });

  it("offers a retry for an answer that failed mid-stream", () => {
    const onRetry = vi.fn();
    render(
      <MessageBubble
        message={assistantMessage({
          error: "The answer could not be completed.",
        })}
        onRetry={onRetry}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("shows copy and retry side by side rather than stacked on a partial answer", () => {
    render(
      <MessageBubble
        message={assistantMessage({
          error: "The answer could not be completed.",
        })}
        onRetry={vi.fn()}
      />,
    );

    // One anchored wrapper holds both controls, so they cannot overlap.
    const retry = screen.getByRole("button", { name: "Retry" });
    const copy = screen.getByRole("button", { name: COPY_LABEL });
    expect(retry.parentElement).toBe(copy.parentElement);
  });

  it("does not offer a retry for a message that streamed cleanly", () => {
    render(<MessageBubble message={assistantMessage()} onRetry={vi.fn()} />);

    expect(
      screen.queryByRole("button", { name: "Retry" }),
    ).not.toBeInTheDocument();
  });

  it("includes the date when the message is not from today", () => {
    render(<MessageBubble message={assistantMessage()} />);

    expect(screen.getByTestId("message-time-m1")).toHaveTextContent("2024");
  });

  it("shows only the time for a message sent today", () => {
    const today = new Date();
    render(<MessageBubble message={assistantMessage({ timestamp: today })} />);

    const stamp = screen.getByTestId("message-time-m1");
    expect(stamp).not.toHaveTextContent(String(today.getFullYear()));
    expect(stamp.textContent).not.toBe("");
  });

  it("hides the copy affordance on a message that failed entirely", () => {
    render(
      <MessageBubble
        message={{
          id: "m2",
          role: "error",
          content: "Something went wrong.",
          timestamp: new Date("2024-01-02T10:30:00Z"),
        }}
        onRetry={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("button", { name: COPY_LABEL }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("namespaces the source ids by message", () => {
    const withSources = (id: string): Message =>
      assistantMessage({ id, sources: [CITATION] });

    render(
      <>
        <MessageBubble message={withSources("m1")} />
        <MessageBubble message={withSources("m2")} />
      </>,
    );

    const toggles = screen.getAllByTestId("sources-toggle");
    expect(toggles[0]).toHaveAttribute("id", "sources-toggle-m1");
    expect(toggles[1]).toHaveAttribute("id", "sources-toggle-m2");
    // Each toggle points at the list that belongs to its own message, which is
    // the whole reason the ids carry the message id.
    expect(toggles[0]).toHaveAttribute("aria-controls", "sources-list-m1");
    expect(toggles[1]).toHaveAttribute("aria-controls", "sources-list-m2");
  });

  it("starts the source list collapsed without hiding the count", () => {
    render(
      <MessageBubble message={assistantMessage({ sources: [CITATION] })} />,
    );

    expect(screen.getByTestId("sources-count")).toHaveTextContent(
      "Sources (1)",
    );
    expect(screen.queryByTestId("sources-list")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("sources-toggle"));

    expect(screen.getByTestId("sources-list")).toBeInTheDocument();
    expect(screen.getByText(CITATION)).toBeInTheDocument();
  });

  it("renders the chrome without the empty bubble shell when bare", () => {
    render(
      <MessageBubble
        bare
        message={assistantMessage({ id: "bare-1", content: "" })}
      />,
    );

    // Nothing has been said yet, so there is no bubble to draw an empty
    // rounded rectangle for, and no action row to hang off it either. The turn
    // still exists, so its timestamp stays.
    expect(
      screen.queryByTestId("message-bubble-bare-1"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: COPY_LABEL }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId(`assistant-text-bare-1`)).not.toBeInTheDocument();
    expect(screen.getByTestId("message-time-bare-1")).toBeInTheDocument();
    expect(screen.getByTestId("avatar-bare-1")).toBeInTheDocument();
  });

  it("keeps the shell for a bare request once the message has content", () => {
    render(
      <MessageBubble
        bare
        message={assistantMessage({ id: "bare-2", content: "Anything yet?" })}
      />,
    );

    expect(screen.getByTestId("message-bubble-bare-2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: COPY_LABEL })).toBeInTheDocument();
  });

  it("shows a caret at the end of the content while it is still streaming", () => {
    render(
      <MessageBubble
        tailCaret
        message={assistantMessage({ id: "live-1", content: "Water damage" })}
      />,
    );

    const caret = screen.getByTestId("tail-caret");
    expect(caret).toBeInTheDocument();
    // The same pulse utility the typing dots use, which the reduced-motion
    // block in globals.css cancels for both.
    expect(caret.className).toContain("animate-pulse");
    expect(caret.parentElement).toBe(screen.getByTestId("assistant-text-live-1"));
  });

  it("leaves the caret out of a message that is not streaming", () => {
    render(<MessageBubble message={assistantMessage()} />);

    expect(screen.queryByTestId("tail-caret")).not.toBeInTheDocument();
  });
});
