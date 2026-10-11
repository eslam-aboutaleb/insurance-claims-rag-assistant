import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  renderHook,
  render,
  screen,
  act,
  waitFor,
  within,
} from "@testing-library/react";
import React from "react";
import { useChatMessages } from "@/hooks/useChatMessages";
import { AuthProvider } from "@/context/AuthContext";
import ToolCallBadge from "@/components/ToolCallBadge";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, confirmClaim } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, confirmClaim: vi.fn() };
});

const queryClient = new QueryClient();

/**
 * A stored conversation the hook loads and renders, so the archived-chat tests
 * exercise the guard against the transcript a reader actually sees.
 */
const ARCHIVED_HISTORY = {
  id: "archived-1",
  title: "Water damage claim",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:05:00Z",
  messages: [
    {
      id: "stored-1",
      role: "user",
      content: "Is a burst pipe covered?",
      timestamp: "2026-01-01T00:00:00Z",
    },
  ],
};

/** Every URL the fetch mock has been asked for since it was last cleared. */
function requestedUrls(): string[] {
  return vi.mocked(global.fetch).mock.calls.map(([url]) => String(url));
}

/** Whether the streaming endpoint was reached, i.e. whether a turn started. */
function streamingRequests(): string[] {
  return requestedUrls().filter((url) => url.includes("/api/v1/chat/stream"));
}

/**
 * Stand in for the archived-conversation endpoints: the stored transcript loads
 * and the streaming endpoint answers 500, so a turn that somehow started would
 * announce itself loudly instead of silently succeeding.
 */
function mockArchivedConversation() {
  // A stored token only survives hydration when it looks like a JWT, and the
  // history only loads for a session the context actually holds.
  window.localStorage.setItem("omnicare_token", "header.payload.signature");
  global.fetch = vi.fn((url: string) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ user_id: "user-123" }),
      } as Response);
    }
    if (url.includes("/api/v1/chat/conversations/")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(ARCHIVED_HISTORY),
      } as Response);
    }
    return Promise.resolve({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ detail: "Server error" }),
    } as Response);
  }) as unknown as typeof fetch;
}

const createWrapper = () => {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(AuthProvider, null, children),
    );
};

beforeEach(() => {
  window.localStorage.setItem("omnicare_token", "fake-token");
  window.localStorage.setItem("omnicare_user_id", "user-123");
  global.fetch = vi.fn((url: string) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ user_id: "user-123" }),
      } as Response);
    }
    return Promise.resolve({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ detail: "Server error" }),
    } as Response);
  });
});

describe("useChatMessages", () => {
  const onNewChat = vi.fn();
  const onMessageSent = vi.fn();
  const onAuthError = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("adds user message then assistant message on send", async () => {
    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    act(() => {
      result.current.handleSendMessage("Hello");
    });

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0].role).toBe("user");
    expect(result.current.messages[0].content).toBe("Hello");
    expect(result.current.messages[1].role).toBe("assistant");
  });

  it("does not send empty message", async () => {
    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    act(() => {
      result.current.handleSendMessage("   ");
    });

    expect(result.current.messages).toHaveLength(0);
  });

  it("isStreaming is true while message is being sent", async () => {
    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    act(() => {
      result.current.handleSendMessage("Hello");
    });

    expect(result.current.isStreaming).toBe(true);
  });

  it("handleStopGeneration stops streaming", async () => {
    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    act(() => {
      result.current.handleSendMessage("Hello");
    });

    expect(result.current.isStreaming).toBe(true);

    act(() => {
      result.current.handleStopGeneration();
    });

    expect(result.current.isStreaming).toBe(false);
  });

  it("isReadOnly is true when historyId is set", async () => {
    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          historyId: "some-id",
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    expect(result.current.isReadOnly).toBe(true);
  });

  it("isReadOnly is false when historyId is not set", async () => {
    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    expect(result.current.isReadOnly).toBe(false);
  });

  it("does not start a turn from an archived conversation", async () => {
    mockArchivedConversation();

    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          historyId: ARCHIVED_HISTORY.id,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    // The archived transcript is on screen before the send is attempted, so the
    // guard is proven against a populated list rather than an empty one.
    await waitFor(() => expect(result.current.messages).toHaveLength(1));

    act(() => {
      result.current.handleSendMessage("One more question");
    });

    expect(result.current.messages).toHaveLength(1);
    expect(streamingRequests()).toHaveLength(0);
    expect(onMessageSent).not.toHaveBeenCalled();
  });

  it("does not retry the last turn from an archived conversation", async () => {
    mockArchivedConversation();

    const { result } = renderHook(
      () =>
        useChatMessages({
          onNewChat,
          historyId: ARCHIVED_HISTORY.id,
          onMessageSent,
          onAuthError,
        }),
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(result.current.messages).toHaveLength(1));

    act(() => {
      result.current.handleRetryLast();
    });

    // Retrying discards the tail of the transcript and appends a fresh bubble,
    // so the surviving single stored message is the whole assertion.
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe("stored-1");
    expect(streamingRequests()).toHaveLength(0);
  });
});

describe("useChatMessages streaming", () => {
  const onNewChat = vi.fn();
  const scrollIntoView = vi.fn();
  const realScrollIntoView = Element.prototype.scrollIntoView;

  /** A readable stream the test releases one frame at a time, so a turn can be
   * paused mid-answer. */
  function createPushStream() {
    const encoder = new TextEncoder();
    const queue: string[] = [];
    let wake: (() => void) | null = null;
    let failure: Error | null = null;
    let closed = false;

    const reader = {
      read: async () => {
        while (queue.length === 0 && !failure && !closed) {
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
        }
        if (failure) throw failure;
        if (queue.length === 0) return { done: true, value: undefined };
        return { done: false, value: encoder.encode(queue.shift() as string) };
      },
      releaseLock: () => {},
      cancel: () => {},
    };

    return {
      response: {
        ok: true,
        status: 200,
        body: { getReader: () => reader },
      } as unknown as Response,
      push(frame: string) {
        queue.push(frame);
        wake?.();
      },
      fail(error: Error) {
        failure = error;
        wake?.();
      },
      close() {
        closed = true;
        wake?.();
      },
    };
  }

  const textDelta = (content: string) =>
    `data: ${JSON.stringify({ type: "text_delta", content })}\n\n`;
  const responseComplete = () =>
    `data: ${JSON.stringify({ type: "response_complete", sources: [], status: "ok" })}\n\n`;
  const toolCall = (tool: string, args: Record<string, unknown>) =>
    `data: ${JSON.stringify({ type: "tool_call", tool, arguments: args })}\n\n`;
  const toolResult = (tool: string, result: unknown) =>
    `data: ${JSON.stringify({ type: "tool_result", tool, result })}\n\n`;

  /** The claim the agent is asking the user to confirm. */
  const PREPARED_CLAIM = {
    policy_number: "POL-1092",
    claim_type: "Water Damage",
    amount: 3500,
    description: "A pipe burst flooded the kitchen yesterday",
  };
  const CONFIRMATION_TOKEN = "8f3c2a54-1c1e-4f6a-9d3b-7b2c1a4e5f60";
  // Derived from the clock rather than a fixed stamp: a hard-coded expiry makes
  // these tests pass while they are fresh and silently disable the confirm
  // button once the date passes, which is exactly how this file broke.
  const EXPIRES_AT = new Date(Date.now() + 15 * 60 * 1000).toISOString();
  const prepareCall = () =>
    toolCall("prepare_claim_submission", PREPARED_CLAIM);
  const prepareResult = () =>
    toolResult("prepare_claim_submission", {
      success: true,
      confirmation_token: CONFIRMATION_TOKEN,
      expires_at: EXPIRES_AT,
      status: "pending",
      message:
        "Your claim has been prepared. Please confirm the submission in the UI " +
        "to complete your claim.",
    });

  /**
   * Stand-in for ChatWindow's scroll container, so the auto-scroll effect and
   * its scroll listener operate on a real element the way they do in the app.
   * Written with ``createElement`` because this suite is a ``.ts`` file.
   */
  function ChatHarness() {
    const chat = useChatMessages({ onNewChat });
    const create = React.createElement;
    return create(
      React.Fragment,
      null,
      create(
        "button",
        {
          type: "button",
          "data-testid": "send",
          onClick: () => chat.handleSendMessage("What is my claim status?"),
        },
        "Send",
      ),
      chat.showScrollButton
        ? create(
            "button",
            {
              type: "button",
              "aria-label": "Scroll to bottom",
              onClick: () => chat.scrollToBottom(),
            },
            "latest",
          )
        : null,
      create(
        "div",
        {
          ref: chat.scrollRef,
          "data-testid": "scroller",
          "data-auth-ready": chat.isLoading ? "no" : "yes",
          "data-phase": chat.streamPhase,
          "data-announcement": chat.statusAnnouncement,
          "data-tools": JSON.stringify(
            chat.messages.flatMap((message) => message.toolCalls ?? []),
          ),
          style: { height: 400, overflowY: "auto" },
        },
        create(
          "div",
          { "data-testid": "transcript" },
          chat.messages.map((message) =>
            create(
              "article",
              {
                key: message.id,
                "data-testid": "turn",
                "data-role": message.role,
              },
              create("p", { "data-testid": "turn-text" }, message.content),
              message.error
                ? create("p", { "data-testid": "turn-error" }, message.error)
                : null,
              // The tool area is where a prepared claim becomes actionable,
              // so the harness renders the same component the bubble does.
              create(ToolCallBadge, {
                toolCalls: message.toolCalls ?? [],
              }),
            ),
          ),
        ),
        create("div", {
          ref: chat.messagesEndRef,
          "data-testid": "messages-end",
        }),
      ),
    );
  }

  beforeEach(() => {
    scrollIntoView.mockClear();
    Element.prototype.scrollIntoView = scrollIntoView;
  });

  afterEach(() => {
    Element.prototype.scrollIntoView = realScrollIntoView;
  });

  /** Mounts the hook with a suspended stream and waits for authentication. */
  async function renderChat(stream: ReturnType<typeof createPushStream>) {
    global.fetch = vi.fn((url: string) => {
      if (url.includes("/api/v1/auth/me")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ user_id: "user-123" }),
        } as Response);
      }
      return Promise.resolve(stream.response);
    }) as unknown as typeof fetch;

    const mounted = render(
      React.createElement(
        QueryClientProvider,
        { client: queryClient },
        React.createElement(
          AuthProvider,
          null,
          React.createElement(ChatHarness),
        ),
      ),
    );

    await waitFor(() =>
      expect(screen.getByTestId("scroller")).toHaveAttribute(
        "data-auth-ready",
        "yes",
      ),
    );

    return mounted;
  }

  /** The abort signal handed to the streaming request, read off the mock. */
  function streamSignal(): AbortSignal | undefined {
    const call = vi
      .mocked(global.fetch)
      .mock.calls.find(([url]) => String(url).includes("/api/v1/chat/stream"));
    return (call?.[1] as RequestInit | undefined)?.signal ?? undefined;
  }

  /** Sends a message and lets the request settle without releasing frames. */
  async function send() {
    await act(async () => {
      screen.getByTestId("send").click();
    });
  }

  /** Releases one SSE frame and flushes the resulting state update. */
  async function deliver(
    stream: ReturnType<typeof createPushStream>,
    frame: string,
  ) {
    await act(async () => {
      stream.push(frame);
    });
  }

  /** Puts the reader far above the bottom of the transcript. */
  function scrollAway() {
    const scroller = screen.getByTestId("scroller");
    Object.defineProperty(scroller, "scrollHeight", {
      configurable: true,
      value: 4000,
    });
    Object.defineProperty(scroller, "clientHeight", {
      configurable: true,
      value: 400,
    });
    Object.defineProperty(scroller, "scrollTop", {
      configurable: true,
      value: 0,
    });
    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
  }

  it("keeps partial assistant text and notes the failure when the stream dies", async () => {
    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, textDelta("Water damage is covered"));

    await act(async () => {
      stream.fail(new Error("connection lost"));
    });

    const turns = screen.getAllByTestId("turn");
    expect(turns).toHaveLength(2);
    // The answer stays an answer: the failure is a note on it, not a replacement.
    expect(turns[1]).toHaveAttribute("data-role", "assistant");
    expect(within(turns[1]).getByTestId("turn-text")).toHaveTextContent(
      "Water damage is covered",
    );
    const note = within(turns[1]).getByTestId("turn-error");
    expect(note.textContent).not.toBe("");
  });

  it("follows the stream with an instant scroll, one the reader's jump is not", async () => {
    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, textDelta("first part"));

    // The follow is instant, so the view keeps up with the deltas rather than
    // trailing an animation re-queued on every one of them.
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "auto" });
    expect(scrollIntoView).not.toHaveBeenCalledWith({ behavior: "smooth" });

    // The reader's own jump back down is a single deliberate gesture, and stays
    // the smooth one.
    scrollAway();
    scrollIntoView.mockClear();
    act(() => {
      screen.getByRole("button", { name: "Scroll to bottom" }).click();
    });
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth" });
  });

  it("aborts the in-flight request when the reader leaves mid-stream", async () => {
    const stream = createPushStream();
    const { unmount } = await renderChat(stream);

    await send();
    await deliver(stream, textDelta("still typing"));

    const signal = streamSignal();
    expect(signal).toBeDefined();
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
  });

  it("does not auto-scroll while the reader has scrolled away from the bottom", async () => {
    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, textDelta("first part"));
    // Still pinned to the bottom, so the stream is followed.
    expect(scrollIntoView).toHaveBeenCalled();

    scrollAway();
    scrollIntoView.mockClear();

    await deliver(stream, textDelta(" and more text"));

    expect(scrollIntoView).not.toHaveBeenCalled();
    // The reader can still ask to jump to the latest.
    expect(
      screen.getByRole("button", { name: "Scroll to bottom" }),
    ).toBeInTheDocument();
  });

  it("resumes following the stream once the reader jumps back to the bottom", async () => {
    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, textDelta("first part"));
    scrollAway();
    scrollIntoView.mockClear();

    await act(async () => {
      screen.getByRole("button", { name: "Scroll to bottom" }).click();
      stream.push(textDelta(" and more text"));
    });

    expect(scrollIntoView).toHaveBeenCalled();

    await deliver(stream, responseComplete());
    await act(async () => {
      stream.close();
    });
  });

  it("tracks tool activity and the tool's return value on the message", async () => {
    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(
      stream,
      toolCall("get_claim_status", { claim_id: "CLM-8821" }),
    );
    await deliver(
      stream,
      toolResult("get_claim_status", {
        found: true,
        claim_id: "CLM-8821",
        status: "Submitted",
      }),
    );
    await deliver(stream, textDelta("Your claim CLM-8821 is submitted."));
    await deliver(stream, responseComplete());

    const scroller = screen.getByTestId("scroller");
    expect(JSON.parse(scroller.dataset.tools ?? "[]")).toEqual([
      {
        name: "get_claim_status",
        arguments: { claim_id: "CLM-8821" },
        result: { found: true, claim_id: "CLM-8821", status: "Submitted" },
      },
    ]);
    const turn = screen.getAllByTestId("turn")[1];
    expect(turn).toHaveTextContent("Your claim CLM-8821 is submitted.");
  });

  it("describes the work in flight instead of one fixed loading message", async () => {
    const stream = createPushStream();
    await renderChat(stream);
    const scroller = screen.getByTestId("scroller");

    await send();
    expect(scroller).toHaveAttribute("data-phase", "searching");

    await deliver(
      stream,
      toolCall("get_claim_status", { claim_id: "CLM-8821" }),
    );
    expect(scroller).toHaveAttribute("data-phase", "preparing");

    await deliver(stream, textDelta("Your claim"));
    expect(scroller).toHaveAttribute("data-phase", "responding");

    await deliver(stream, responseComplete());
    // The terminal frame is dispatched as it arrives; the turn only counts as
    // finished once the stream is closed.
    expect(scroller).toHaveAttribute(
      "data-announcement",
      "OmniCare is responding",
    );

    await act(async () => {
      stream.close();
    });
    expect(scroller).toHaveAttribute("data-announcement", "Answer ready");
  });

  it("captures a prepared claim as structured state on the message that prepared it", async () => {
    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, prepareCall());
    await deliver(stream, prepareResult());
    await deliver(stream, textDelta("Please confirm your claim in the UI."));
    await deliver(stream, responseComplete());

    const scroller = screen.getByTestId("scroller");
    expect(JSON.parse(scroller.dataset.tools ?? "[]")).toEqual([
      {
        name: "prepare_claim_submission",
        arguments: PREPARED_CLAIM,
        result: {
          kind: "pending_claim_confirmation",
          token: CONFIRMATION_TOKEN,
          expiresAt: EXPIRES_AT,
          status: "pending",
          message:
            "Your claim has been prepared. Please confirm the submission in " +
            "the UI to complete your claim.",
          claimData: PREPARED_CLAIM,
        },
      },
    ]);

    // The prepared claim belongs to the turn the user asked in, not to any
    // other assistant message in the transcript, and it is on screen without
    // anything having to be clicked to reveal it.
    const turn = screen.getAllByTestId("turn")[1];
    const card = within(turn).getByTestId("claim-confirm-card");
    expect(card).toBeVisible();
    expect(card).toHaveTextContent("$3,500");
  });

  it("replaces the prepared claim with a receipt once it is confirmed", async () => {
    vi.mocked(confirmClaim).mockResolvedValue({
      success: true,
      claim_id: "CLM-TEST123",
      status: "Submitted",
      message: "Your claim CLM-TEST123 has been successfully submitted.",
    });
    // A stored token only survives hydration when it looks like a JWT.
    window.localStorage.setItem("omnicare_token", "header.payload.signature");

    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, prepareCall());
    await deliver(stream, prepareResult());
    await deliver(stream, responseComplete());

    await act(async () => {
      screen.getByTestId("confirm-claim-button").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("claim-receipt")).toHaveTextContent(
        "CLM-TEST123",
      ),
    );

    const scroller = screen.getByTestId("scroller");
    expect(JSON.parse(scroller.dataset.tools ?? "[]")).toEqual([
      {
        name: "prepare_claim_submission",
        arguments: PREPARED_CLAIM,
        result: {
          kind: "claim_confirmation_receipt",
          claimId: "CLM-TEST123",
          status: "Submitted",
          message: "Your claim CLM-TEST123 has been successfully submitted.",
        },
      },
    ]);
    expect(screen.queryByTestId("claim-confirm-card")).not.toBeInTheDocument();
    expect(confirmClaim).toHaveBeenCalledWith(
      "header.payload.signature",
      CONFIRMATION_TOKEN,
    );
  });

  it("leaves the prepared claim in place when the confirmation is refused", async () => {
    vi.mocked(confirmClaim).mockRejectedValue(
      new ApiError(
        "Invalid, expired, or already used confirmation token.",
        400,
      ),
    );

    const stream = createPushStream();
    await renderChat(stream);

    await send();
    await deliver(stream, prepareCall());
    await deliver(stream, prepareResult());
    await deliver(stream, responseComplete());

    await act(async () => {
      screen.getByTestId("confirm-claim-button").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("claim-confirm-error")).toBeInTheDocument(),
    );
    const scroller = screen.getByTestId("scroller");
    expect(JSON.parse(scroller.dataset.tools ?? "[]")[0].result).toMatchObject({
      kind: "pending_claim_confirmation",
      token: CONFIRMATION_TOKEN,
    });
  });
});
