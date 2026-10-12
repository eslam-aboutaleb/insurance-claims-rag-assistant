import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
  act,
} from "@testing-library/react";
import {
  MemoryRouter,
  Routes,
  Route,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "@/context/AuthContext";
import ChatPage from "@/pages/ChatPage";

const CONVERSATIONS = [
  {
    id: "aaaaaaaa-1111-1111-1111-111111111111",
    title: "Old convo about water damage",
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
  },
  {
    id: "bbbbbbbb-2222-2222-2222-222222222222",
    title: "Old convo about filing a claim",
    created_at: "2024-01-02T00:00:00Z",
    updated_at: "2024-01-02T00:00:00Z",
  },
  {
    id: "cccccccc-3333-3333-3333-333333333333",
    title: "Archived convo with no messages",
    created_at: "2024-01-03T00:00:00Z",
    updated_at: "2024-01-03T00:00:00Z",
  },
];

const MESSAGES: Record<
  string,
  { id: string; role: string; content: string; timestamp: string }[]
> = {
  "aaaaaaaa-1111-1111-1111-111111111111": [
    {
      id: "m1",
      role: "user",
      content: "Is water damage covered?",
      timestamp: "2024-01-01T00:00:00Z",
    },
    {
      id: "m2",
      role: "assistant",
      content: "Water damage is covered for sudden events.",
      timestamp: "2024-01-01T00:00:01Z",
    },
  ],
  "bbbbbbbb-2222-2222-2222-222222222222": [
    {
      id: "m3",
      role: "user",
      content: "How do I file a claim?",
      timestamp: "2024-01-02T00:00:00Z",
    },
  ],
  "cccccccc-3333-3333-3333-333333333333": [],
};

const JWT = "header.payload.signature";

const WATER_CONVERSATION = "aaaaaaaa-1111-1111-1111-111111111111";
const CLAIM_CONVERSATION = "bbbbbbbb-2222-2222-2222-222222222222";
const EMPTY_CONVERSATION = "cccccccc-3333-3333-3333-333333333333";

/** A fetch-shaped response for the mocked conversations API. */
function respond(
  body: unknown,
  init: { ok?: boolean; status?: number } = {},
): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: () => Promise.resolve(body),
  } as Response;
}

function currentPath(): string {
  return screen.getByTestId("current-path").textContent ?? "";
}

function installFetch(): void {
  // A fresh copy per render, so a delete in one test cannot leak into the
  // conversation list another test asserts on.
  const conversations = CONVERSATIONS.map((c) => ({ ...c }));
  global.fetch = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve(respond({ user_id: "user-123" }));
    }
    if (url.endsWith("/api/v1/chat/conversations")) {
      return Promise.resolve(respond({ conversations }));
    }
    const detail = url.match(/\/api\/v1\/chat\/conversations\/([^/?#]+)/);
    if (detail) {
      const id = detail[1];
      if (init?.method === "DELETE") {
        const index = conversations.findIndex((c) => c.id === id);
        if (index >= 0) conversations.splice(index, 1);
        return Promise.resolve(respond(null, { ok: true, status: 204 }));
      }
      if (init?.method === "PATCH") {
        const meta = conversations.find((c) => c.id === id);
        return Promise.resolve(respond(meta));
      }
      const meta = conversations.find((c) => c.id === id);
      if (!meta) {
        return Promise.resolve(
          respond({ detail: "Conversation not found" }, { status: 404 }),
        );
      }
      return Promise.resolve(
        respond({ ...meta, messages: MESSAGES[id] ?? [] }),
      );
    }
    if (url.endsWith("/api/v1/chat/reset") && init?.method === "POST") {
      return Promise.resolve(respond({}));
    }
    return Promise.resolve(respond({}));
  }) as unknown as typeof fetch;
}

/**
 * Mock the conversations API with one conversation whose history resolves only
 * when the test releases it, so the in-flight state can be observed instead of
 * raced.
 *
 * @param historyId - Conversation whose detail request is held open.
 * @returns The release function the test calls once it has asserted.
 */
function installFetchWithPendingHistory(historyId: string): {
  releaseHistory: () => void;
} {
  let releaseHistory: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    releaseHistory = resolve;
  });

  global.fetch = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve(respond({ user_id: "user-123" }));
    }
    if (url.endsWith("/api/v1/chat/conversations")) {
      return Promise.resolve(respond({ conversations: CONVERSATIONS }));
    }
    if (url.includes(`/api/v1/chat/conversations/${historyId}`)) {
      return gate.then(() =>
        respond({
          ...CONVERSATIONS.find((c) => c.id === historyId),
          messages: MESSAGES[historyId] ?? [],
        }),
      );
    }
    if (url.endsWith("/api/v1/chat/reset") && init?.method === "POST") {
      return Promise.resolve(respond({}));
    }
    return Promise.resolve(respond({}));
  }) as unknown as typeof fetch;

  return { releaseHistory };
}

/**
 * Mock the conversations API so one conversation's history fails, the way a
 * history request does when it is throttled or dropped.
 *
 * @param historyId - Conversation whose detail request is made to fail.
 */
function installFetchWithFailingHistory(historyId: string): void {
  global.fetch = vi.fn((url: string) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve(respond({ user_id: "user-123" }));
    }
    if (url.endsWith("/api/v1/chat/conversations")) {
      return Promise.resolve(respond({ conversations: CONVERSATIONS }));
    }
    const detail = url.match(/\/api\/v1\/chat\/conversations\/([^/?#]+)/);
    if (detail) {
      const id = detail[1];
      if (id === historyId) {
        return Promise.resolve(
          respond({ detail: "History unavailable" }, { status: 500 }),
        );
      }
      const meta = CONVERSATIONS.find((c) => c.id === id);
      if (!meta) {
        return Promise.resolve(
          respond({ detail: "Conversation not found" }, { status: 404 }),
        );
      }
      return Promise.resolve(
        respond({ ...meta, messages: MESSAGES[id] ?? [] }),
      );
    }
    return Promise.resolve(respond({}));
  }) as unknown as typeof fetch;
}

/**
 * Report every media query as matching the phone layout.
 *
 * jsdom has no media-query engine, so the breakpoint the drawer logic reads has
 * to be supplied by the test; without this stub the page reports the wider
 * layout, where nothing is hidden.
 */
function installMobileViewport(): void {
  window.matchMedia = vi.fn((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/** A readable stream the test releases one frame at a time. */
function createPushStream() {
  const encoder = new TextEncoder();
  const queue: string[] = [];
  let wake: (() => void) | null = null;
  let closed = false;

  const reader = {
    read: async () => {
      while (queue.length === 0 && !closed) {
        await new Promise<void>((resolve) => {
          wake = resolve;
        });
      }
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
    close() {
      closed = true;
      wake?.();
    },
  };
}

/** One SSE frame carrying a fragment of the assistant's answer. */
function textDeltaFrame(content: string): string {
  return `data: ${JSON.stringify({ type: "text_delta", content })}\n\n`;
}

/** One SSE frame terminating the turn. */
function responseCompleteFrame(): string {
  return `data: ${JSON.stringify({
    type: "response_complete",
    sources: [],
    status: "ok",
  })}\n\n`;
}

/** Mock the API so a started turn streams from the supplied push stream. */
function installStreamingFetch(
  stream: ReturnType<typeof createPushStream>,
): void {
  global.fetch = vi.fn((url: string) => {
    if (url.includes("/api/v1/auth/me")) {
      return Promise.resolve(respond({ user_id: "user-123" }));
    }
    if (url.endsWith("/api/v1/chat/conversations")) {
      return Promise.resolve(respond({ conversations: CONVERSATIONS }));
    }
    if (url.includes("/api/v1/chat/stream")) {
      return Promise.resolve(stream.response);
    }
    return Promise.resolve(respond({}));
  }) as unknown as typeof fetch;
}

/** Releases one SSE frame and flushes the state update it produces. */
async function deliverFrame(
  stream: ReturnType<typeof createPushStream>,
  frame: string,
): Promise<void> {
  await act(async () => {
    stream.push(frame);
  });
}

/** Every bubble shell currently rendered, whatever produced it. */
function renderedBubbles(): NodeListOf<Element> {
  return document.querySelectorAll('[data-testid^="message-bubble-"]');
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

/** Renders ChatPage and lets a test drive navigation the way a browser would. */
function NavigationBridge({
  onReady,
}: {
  onReady: (navigate: (to: string) => void) => void;
}) {
  const navigate = useNavigate();
  onReady(navigate);
  return null;
}

function renderChatPage(initialEntry = "/chat") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  let navigate: (to: string) => void = () => {};

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <LocationProbe />
          <NavigationBridge onReady={(n) => (navigate = n)} />
          <Routes>
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/chat/:conversationId" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );

  return { ...utils, queryClient, navigateTo: (to: string) => navigate(to) };
}

let originalMatchMedia: typeof window.matchMedia;

beforeEach(() => {
  originalMatchMedia = window.matchMedia;
  window.localStorage.setItem("omnicare_token", JWT);
  window.localStorage.setItem("omnicare_user_id", "user-123");
  installFetch();
});

afterEach(() => {
  window.matchMedia = originalMatchMedia;
});

describe("ChatPage conversation sidebar navigation", () => {
  it("lists the current user's old conversations in the sidebar", async () => {
    renderChatPage("/chat");

    const history = await screen.findByTestId("chat-history");
    await waitFor(() => {
      expect(history.querySelectorAll("button")).toHaveLength(
        CONVERSATIONS.length,
      );
    });
    expect(
      screen.getByText("Old convo about water damage"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Old convo about filing a claim"),
    ).toBeInTheDocument();
    expect(screen.queryByText("No conversations yet")).not.toBeInTheDocument();
  });

  it("navigates to /chat/:conversationId and loads history when a conversation is clicked", async () => {
    renderChatPage("/chat");

    fireEvent.click(await screen.findByText("Old convo about filing a claim"));

    await waitFor(() => {
      expect(currentPath()).toBe("/chat/bbbbbbbb-2222-2222-2222-222222222222");
    });
    await waitFor(() => {
      expect(screen.getByText("How do I file a claim?")).toBeInTheDocument();
    });
  });

  it("keeps every old conversation reachable from any conversation", async () => {
    renderChatPage("/chat");

    fireEvent.click(await screen.findByText("Old convo about water damage"));
    await waitFor(() =>
      expect(currentPath()).toBe("/chat/aaaaaaaa-1111-1111-1111-111111111111"),
    );

    const history = screen.getByTestId("chat-history");
    expect(history.querySelectorAll("button")).toHaveLength(
      CONVERSATIONS.length,
    );
    expect(
      screen.getByText("Old convo about filing a claim"),
    ).toBeInTheDocument();
  });

  it("swaps the loaded history when a different conversation is clicked", async () => {
    renderChatPage("/chat");

    fireEvent.click(await screen.findByText("Old convo about water damage"));
    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Old convo about filing a claim"));
    await waitFor(() => {
      expect(currentPath()).toBe("/chat/bbbbbbbb-2222-2222-2222-222222222222");
    });
    await waitFor(() => {
      expect(screen.getByText("How do I file a claim?")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Is water damage covered?"),
    ).not.toBeInTheDocument();
  });

  it("remounts the chat window when the route changes without a sidebar click", async () => {
    const { navigateTo } = renderChatPage("/chat");

    fireEvent.click(await screen.findByText("Old convo about water damage"));
    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });

    act(() => {
      navigateTo("/chat/bbbbbbbb-2222-2222-2222-222222222222");
    });

    await waitFor(() => {
      expect(currentPath()).toBe("/chat/bbbbbbbb-2222-2222-2222-222222222222");
    });
    await waitFor(() => {
      expect(screen.getByText("How do I file a claim?")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Is water damage covered?"),
    ).not.toBeInTheDocument();
  });

  it("does not leak the previous conversation's scroll state on a route-only change", async () => {
    const { navigateTo } = renderChatPage("/chat");

    fireEvent.click(await screen.findByText("Old convo about water damage"));
    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });

    const scroller = screen.getByTestId("messages-container");
    Object.defineProperty(scroller, "scrollHeight", {
      configurable: true,
      value: 1000,
    });
    Object.defineProperty(scroller, "clientHeight", {
      configurable: true,
      value: 400,
    });
    fireEvent.scroll(scroller, { target: { scrollTop: 0 } });

    await screen.findByRole("button", { name: "Scroll to bottom" });

    act(() => {
      navigateTo("/chat/bbbbbbbb-2222-2222-2222-222222222222");
    });

    await waitFor(() => {
      expect(currentPath()).toBe("/chat/bbbbbbbb-2222-2222-2222-222222222222");
    });
    await waitFor(() => {
      expect(screen.getByText("How do I file a claim?")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: "Scroll to bottom" }),
    ).not.toBeInTheDocument();
  });

  it("loads history on a direct deep link to an old conversation", async () => {
    renderChatPage("/chat/aaaaaaaa-1111-1111-1111-111111111111");

    expect(currentPath()).toBe("/chat/aaaaaaaa-1111-1111-1111-111111111111");
    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Water damage is covered for sudden events."),
    ).toBeInTheDocument();
  });

  it("keeps the old conversations listed and clears history on New Chat", async () => {
    renderChatPage("/chat/aaaaaaaa-1111-1111-1111-111111111111");
    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("new-chat-button"));

    await waitFor(() => expect(currentPath()).toBe("/chat"));
    expect(
      screen.queryByText("Is water damage covered?"),
    ).not.toBeInTheDocument();
    const history = await screen.findByTestId("chat-history");
    await waitFor(() => {
      expect(history.querySelectorAll("button")).toHaveLength(
        CONVERSATIONS.length,
      );
    });
  });

  it("leaves the page when the conversation being read is deleted", async () => {
    renderChatPage(`/chat/${WATER_CONVERSATION}`);
    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });

    fireEvent.pointerOver(
      screen.getByTestId(`conversation-row-${WATER_CONVERSATION}`),
    );
    fireEvent.click(screen.getByTestId(`delete-button-${WATER_CONVERSATION}`));
    fireEvent.click(screen.getByTestId(`delete-confirm-${WATER_CONVERSATION}`));

    // The transcript is gone, so the page leaves it rather than holding a
    // view whose history 404s on the next load.
    await waitFor(() => expect(currentPath()).toBe("/chat"));

    // The list refreshes without the deleted conversation, and the page still
    // renders the welcome state instead of the removed transcript.
    const history = await screen.findByTestId("chat-history");
    await waitFor(() => {
      expect(history.querySelectorAll("button")).toHaveLength(
        CONVERSATIONS.length - 1,
      );
    });
    expect(
      screen.queryByText("Is water damage covered?"),
    ).not.toBeInTheDocument();
  });
});

describe("ChatPage chat window integrity", () => {
  it("shows a loading state and no suggestion chips while an archived conversation loads", async () => {
    const { releaseHistory } =
      installFetchWithPendingHistory(WATER_CONVERSATION);

    renderChatPage(`/chat/${WATER_CONVERSATION}`);

    await screen.findByTestId("history-loading");
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Check my recent claims"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("What is covered under water damage?"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("File a new claim")).not.toBeInTheDocument();

    await act(async () => {
      releaseHistory();
    });

    await waitFor(() => {
      expect(screen.getByText("Is water damage covered?")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("history-loading")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Check my recent claims"),
    ).not.toBeInTheDocument();
  });

  it("reports an empty archived conversation and hides the composer", async () => {
    renderChatPage(`/chat/${EMPTY_CONVERSATION}`);

    await screen.findByTestId("empty-archived-state");
    expect(
      screen.getByText("This conversation has no messages yet."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-textarea")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-input-wrapper")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Check my recent claims"),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/This is an archived chat\./)).toBeInTheDocument();
  });

  it("keeps the welcome state and its chips for a new chat", async () => {
    renderChatPage("/chat");

    expect(await screen.findByTestId("empty-state")).toBeInTheDocument();
    expect(screen.getByText("Check my recent claims")).toBeInTheDocument();
    expect(screen.getByTestId("chat-textarea")).toBeInTheDocument();
  });

  it("takes the transcript out of the accessibility tree while the mobile sidebar is open", async () => {
    installMobileViewport();
    renderChatPage("/chat");

    const chatContainer = await screen.findByTestId("chat-container");
    expect(chatContainer).not.toHaveAttribute("inert");
    expect(chatContainer).not.toHaveAttribute("aria-hidden");

    fireEvent.click(screen.getByTestId("mobile-menu-toggle"));

    expect(chatContainer).toHaveAttribute("inert", "");
    expect(chatContainer).toHaveAttribute("aria-hidden", "true");
    // The drawer itself is the visible pane, so it stays reachable.
    const sidebarContainer = screen.getByTestId("sidebar-container");
    expect(sidebarContainer).not.toHaveAttribute("inert");
    expect(sidebarContainer).not.toHaveAttribute("aria-hidden");
  });

  it("takes the closed mobile sidebar out of the accessibility tree", async () => {
    installMobileViewport();
    renderChatPage("/chat");

    const sidebarContainer = await screen.findByTestId("sidebar-container");
    expect(sidebarContainer).toHaveAttribute("inert", "");
    expect(sidebarContainer).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByTestId("chat-container")).not.toHaveAttribute("inert");
  });

  it("keeps both panes interactive on a desktop viewport when the sidebar is open", async () => {
    renderChatPage("/chat");
    await screen.findByTestId("chat-history");

    fireEvent.click(screen.getByTestId("mobile-menu-toggle"));

    await screen.findByTestId("sidebar-overlay");
    expect(screen.getByTestId("chat-container")).not.toHaveAttribute("inert");
    expect(screen.getByTestId("chat-container")).not.toHaveAttribute(
      "aria-hidden",
    );
    expect(screen.getByTestId("sidebar-container")).not.toHaveAttribute(
      "inert",
    );
  });

  it("scopes the history-load failure to the chat area and leaves the sidebar usable", async () => {
    installFetchWithFailingHistory(WATER_CONVERSATION);
    renderChatPage(`/chat/${WATER_CONVERSATION}`);

    await screen.findByText("Failed to load conversation");
    const chatContainer = screen.getByTestId("chat-container");
    expect(chatContainer).toContainElement(
      screen.getByTestId("history-load-error"),
    );
    expect(screen.getByTestId("sidebar-container")).not.toHaveAttribute(
      "inert",
    );

    const history = screen.getByTestId("chat-history");
    fireEvent.click(
      within(history).getByText("Old convo about filing a claim"),
    );

    await waitFor(() =>
      expect(currentPath()).toBe(`/chat/${CLAIM_CONVERSATION}`),
    );
    await waitFor(() =>
      expect(screen.getByText("How do I file a claim?")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("history-load-error")).not.toBeInTheDocument();
  });

  it("pads the transcript so the first message clears the mobile menu button", async () => {
    renderChatPage("/chat");

    const messagesContainer = await screen.findByTestId("messages-container");
    expect(messagesContainer.className).toContain("pl-16");
    expect(messagesContainer.className).toContain("pt-16");
    expect(messagesContainer.className).toContain("md:pl-6");
    expect(messagesContainer.className).toContain("md:pt-6");
  });

  it("drops the empty bubble shell and marks the last message only while text streams", async () => {
    const stream = createPushStream();
    installStreamingFetch(stream);
    renderChatPage("/chat");

    fireEvent.change(await screen.findByTestId("chat-textarea"), {
      target: { value: "Is water damage covered?" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    // The turn is open but no token has landed, so the waiting state is the
    // typing indicator alone: the only shell on screen is the user's own.
    await screen.findByTestId("loading-indicator");
    expect(renderedBubbles()).toHaveLength(1);
    expect(screen.queryByTestId("tail-caret")).not.toBeInTheDocument();

    await deliverFrame(
      stream,
      textDeltaFrame("Water damage is covered for sudden events."),
    );

    expect(screen.getByTestId("tail-caret")).toBeInTheDocument();
    expect(renderedBubbles()).toHaveLength(2);

    await deliverFrame(stream, responseCompleteFrame());

    // A turn settles when the connection closes, exactly as it does in the app.
    await act(async () => {
      stream.close();
    });

    await waitFor(() =>
      expect(screen.queryByTestId("loading-indicator")).not.toBeInTheDocument(),
    );
    expect(
      screen.getByText("Water damage is covered for sudden events."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("tail-caret")).not.toBeInTheDocument();
  });
});
